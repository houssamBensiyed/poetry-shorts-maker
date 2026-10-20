from dataclasses import asdict, replace
import json
import logging
from pathlib import Path
import shutil
import threading
import uuid
from app.config import check_job_config, ROOT
from app.state import State, RunLock
from app.jobs import JobTable
from app.downloads import Downloader
from app.images import download_images
from app.uploads import Uploader
from app.transcription import Transcriber
from app.footage import audio_duration, discover
from app.timeline import sentences_from_words, build_plan, Source
from app.video_builder import render, check_cancel, Cancelled

log = logging.getLogger('video_builder')


class Processor:
    def __init__(self, config):
        self.config = config
        self.state = State()
        self.stop_event = threading.Event()
        self.guard = threading.RLock()
        self.thread = None
        self.refreshing = False
        # One lock per online table, shared by dashboard and API.
        self.run_lock = RunLock(config['baserow']['api_url'].rstrip('/')+'/'+str(config['baserow']['table_id']))
        self.transcriber = Transcriber(config['whisper'])

    def update(self, **values):
        self.state.update(**values)
        if self.run_lock.handle:
            self.run_lock.publish(self.state.snapshot())

    def status(self):
        with self.guard:
            if not self.run_lock.handle and self.run_lock.busy():
                return {**self.run_lock.external_state(),'external_process':True}
            return {**self.state.snapshot(),'external_process':False}

    def start(self):
        with self.guard:
            if self.refreshing or self.thread and self.thread.is_alive():
                return False
            if not self.run_lock.acquire():
                return False
            self.stop_event.clear()
            self.update(status='processing',stage='Connecting to Baserow',error='',render_progress=None)
            self.thread = threading.Thread(target=self._run,name='video-processor',daemon=False)
            try:
                self.thread.start()
            except Exception:
                self.run_lock.release()
                raise
            return True

    def stop(self):
        self.stop_event.set()
        log.info('Stop requested; FFmpeg stops immediately, network/Whisper stops at its next safe checkpoint')

    def close(self):
        self.stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join()

    def refresh(self):
        with self.guard:
            if self.refreshing or self.thread and self.thread.is_alive() or self.run_lock.busy():
                return self.status()
            self.refreshing = True
        try:
            jobs = JobTable(self.config['baserow']).jobs()
            self.state.update(**self._counts(jobs),status='idle',stage='Jobs refreshed',error='')
            return self.status()
        finally:
            with self.guard:
                self.refreshing = False

    @staticmethod
    def _counts(jobs):
        return dict(pending=sum(j.status=='NOT_PROCESSED' for j in jobs),
                    completed=sum(j.status in {'DONE','COMPLETED'} for j in jobs),
                    failed=sum(j.status=='FAILED' for j in jobs))

    def stage(self, name):
        check_cancel(self.stop_event)
        self.update(stage=name,render_progress=None)
        log.info('%s',name)

    def _run(self):
        try:
            check_job_config(self.config)
            sheet = JobTable(self.config['baserow'],self.stop_event)
            jobs = sheet.jobs()
            self.update(**self._counts(jobs))
            pending = [j for j in jobs if j.status=='NOT_PROCESSED']
            stuck = sum(j.status=='PROCESSING' for j in jobs)
            if stuck:
                log.warning('%s rows already PROCESSING: inspect output and use documented manual recovery',stuck)
            log.info('Found %d pending jobs',len(pending))
            sources = [Source(url,'image') for url in sheet.image_urls()] if pending else []
            if pending and self.config['footage']['use_local_assets']:
                sources += discover(self.config['footage'])
            downloader = Downloader(self.config['downloads'],self.stop_event)
            for job in pending:
                if self.stop_event.is_set():
                    break
                self._job(job,sheet,downloader,sources)
            self.update(status='idle',stage='Stopped' if self.stop_event.is_set() else 'Run finished',
                        current_job=None,voiceover='',render_progress=None)
        except Cancelled:
            self.update(status='idle',stage='Stopped',current_job=None,render_progress=None)
        except Exception as exc:
            log.exception('Processing run failed')
            self.update(status='error',stage='Run failed',error=str(exc)[:1000],render_progress=None)
        finally:
            with self.guard:
                self.run_lock.release()

    def _job(self, job, sheet, downloader, sources):
        token = uuid.uuid4().hex
        work = Path(self.config['paths']['temp'])/f'row-{job.row}-{token}'
        work.mkdir(parents=True,exist_ok=False)
        output = Path(self.config['paths']['output'])/f'row-{job.row}-{token}.mp4'
        audio = Path(self.config['paths']['buffer'])/f'row-{job.row}-{token}.audio'
        completed_output = ''
        uploaded_url = ''
        marked = False
        self.update(current_job=job.id or f'Row {job.row}',voiceover=job.voice_link,
                    pending=max(0,self.state.snapshot()['pending']-1))
        try:
            sheet.update(job,'PROCESSING',stop=self.stop_event)
            marked = True
            self.stage('Downloading')
            downloader.download(job.voice_link,audio)
            duration = audio_duration(audio)
            self.stage('Transcribing')
            transcript = self.transcriber.transcribe(audio,duration,self.stop_event,work)
            (work/'transcript.json').write_text(json.dumps(asdict(transcript),ensure_ascii=False,indent=2))
            sentences = sentences_from_words(transcript.words,duration)
            log.info('Detected %d sentences',len(sentences))
            self.stage('Planning footage')
            f = self.config['footage']
            plan = build_plan(sentences,sources,duration,f['minimum_words_for_new_clip'],f['random_seed'],f['avoid_immediate_repetition'])
            for sentence in sentences:
                log.debug('Sentence %r has %d words: %s',sentence.text,len(sentence.words),
                          'switching footage' if len(sentence.words)>=f['minimum_words_for_new_clip'] else 'keeping current footage')
            log.info('Created %d footage intervals',len(plan.footage))
            remote_urls = list(dict.fromkeys(interval.source.path for interval in plan.footage
                                             if interval.source.path.startswith(('http://','https://'))))
            if remote_urls:
                self.stage('Downloading selected images')
                image_directory = Path(self.config['paths']['image_buffer'])/f'row-{job.row}-{token}'
                selected = download_images(remote_urls,image_directory,downloader,self.stop_event)
                mapping = dict(zip(remote_urls,selected))
                plan = replace(plan,footage=tuple(replace(interval,source=mapping.get(interval.source.path,interval.source))
                                                 for interval in plan.footage))
                log.info('Downloaded %d distinct selected images to the local buffer',len(selected))
            self.stage('Generating captions')
            for caption in plan.captions:
                log.debug('Caption %r persists until %.3fs',caption.sentence.text,caption.end)
            self.stage('Rendering')
            last = [-1]
            def progress(value):
                self.update(render_progress=round(value,1))
                bucket = int(value)//10
                if bucket>last[0]:
                    log.info('Rendering: %.0f%%',value)
                    last[0] = bucket
            render(plan,audio,output,work,self.config,self.stop_event,progress)
            # The MP4 is already validated and complete; preserve it if the table update fails.
            completed_output = str(output.relative_to(ROOT)) if output.is_relative_to(ROOT) else str(output)
            (work/'rendered.json').write_text(json.dumps({'output':completed_output,'row':job.row,'audio':str(audio)}))
            self.stage('Uploading to Cloudinary')
            uploader = Uploader(self.config['cloudinary'],self.stop_event)
            (work/'upload.json').write_text(json.dumps({'public_id':uploader.public_id(output),'output':completed_output,'row':job.row}))
            last_upload = [-1]
            def upload_progress(value):
                if int(value)//10 > last_upload[0]:
                    log.info('Upload: %.0f%%',value)
                    last_upload[0] = int(value)//10
            uploaded = uploader.upload(output,upload_progress)
            uploaded_url = uploaded['url']
            (work/'uploaded.json').write_text(json.dumps({**uploaded,'output':completed_output,'row':job.row}))
            # Commit an acknowledged upload even if Stop arrived with its final response.
            self.update(stage='Updating Baserow',render_progress=None)
            sheet.update(job,'DONE',output=completed_output,uploaded_url=uploaded_url)
            self.update(completed=self.state.snapshot()['completed']+1)
            log.info('Job row %s completed: %s',job.row,completed_output)
            try:
                shutil.rmtree(work)
            except OSError:
                log.warning("Completed output is safe, but temporary files could not be removed: %s",work)
        except Exception as exc:
            cancelled = isinstance(exc,Cancelled)
            if cancelled:
                log.warning('Job row %s interrupted; set FAILED row to NOT_PROCESSED to recover',job.row)
            else:
                log.exception('Job row %s failed; diagnostics retained at %s',job.row,work)
            error = str(exc)[:1000]
            (work/'failure.json').write_text(json.dumps({'row':job.row,'error':error,'output':completed_output,'uploaded_url':uploaded_url,'audio':str(audio)}))
            # Keep a complete rendered MP4 for recovery if the table could not be updated.
            if not completed_output:
                output.unlink(missing_ok=True)
            try:
                if marked:
                    sheet.update(job,'FAILED',output=completed_output,uploaded_url=uploaded_url,error=error)
            except Exception:
                log.exception('Could not update failed row %s; use manual recovery in README',job.row)
            self.update(failed=self.state.snapshot()['failed']+1,error=error)
