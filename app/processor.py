from dataclasses import asdict
import json
import logging
from pathlib import Path
import shutil
import threading
import uuid
from app.config import check_job_config
from app.state import State, RunLock
from app.sheets import Sheets
from app.drive import Drive
from app.transcription import Transcriber
from app.footage import audio_duration, discover
from app.timeline import sentences_from_words, build_plan
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
        # Spreadsheet-wide: different worksheet/config paths cannot run concurrently.
        self.run_lock = RunLock(config['google']['spreadsheet_id'] or 'unconfigured')
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
            self.update(status='processing',stage='Connecting to Google Sheets',error='',render_progress=None)
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
            google = self.config['google']
            if not google['spreadsheet_id']:
                raise ValueError('Set google.spreadsheet_id in config.yaml')
            jobs = Sheets(google).jobs()
            self.state.update(**self._counts(jobs),status='idle',stage='Jobs refreshed',error='')
            return self.status()
        finally:
            with self.guard:
                self.refreshing = False

    @staticmethod
    def _counts(jobs):
        return dict(pending=sum(j.status=='NOT_PROCESSED' for j in jobs),
                    completed=sum(j.status=='COMPLETED' for j in jobs),
                    failed=sum(j.status=='FAILED' for j in jobs))

    def stage(self, name):
        check_cancel(self.stop_event)
        self.update(stage=name,render_progress=None)
        log.info('%s',name)

    def _run(self):
        try:
            check_job_config(self.config)
            sources = discover(self.config['footage'])
            sheet = Sheets(self.config['google'],self.stop_event)
            jobs = sheet.jobs()
            self.update(**self._counts(jobs))
            pending = [j for j in jobs if j.status=='NOT_PROCESSED']
            stuck = sum(j.status=='PROCESSING' for j in jobs)
            if stuck:
                log.warning('%s rows already PROCESSING: inspect output and use documented manual recovery',stuck)
            log.info('Found %d pending jobs',len(pending))
            drive = Drive(self.config['google'],self.stop_event)
            for job in pending:
                if self.stop_event.is_set():
                    break
                self._job(job,sheet,drive,sources)
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

    def _job(self, job, sheet, drive, sources):
        token = uuid.uuid4().hex
        work = Path(self.config['paths']['temp'])/f'row-{job.row}-{token}'
        work.mkdir(parents=True,exist_ok=False)
        output = Path(self.config['paths']['output'])/f'row-{job.row}-{token}.mp4'
        audio = work/'voiceover.audio'
        uploaded_url = ''
        marked = False
        self.update(current_job=job.id or f'Row {job.row}',voiceover=job.voice_link,
                    pending=max(0,self.state.snapshot()['pending']-1))
        try:
            sheet.update(job,'PROCESSING',stop=self.stop_event)
            marked = True
            self.stage('Downloading')
            drive.download(job.voice_link,audio)
            duration = audio_duration(audio)
            self.stage('Transcribing')
            transcript = self.transcriber.transcribe(audio,duration,self.stop_event)
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
            self.stage('Uploading')
            uploaded_url = drive.upload(output)
            # Commit a completed upload even if Stop arrives now; do not orphan its URL.
            self.update(stage='Updating Google Sheets',render_progress=None)
            (work/'uploaded.json').write_text(json.dumps({'url':uploaded_url,'row':job.row,'output':str(output)}))
            sheet.update(job,'COMPLETED',output=uploaded_url)
            self.update(completed=self.state.snapshot()['completed']+1)
            log.info('Job row %s completed: %s',job.row,uploaded_url)
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
            (work/'failure.json').write_text(json.dumps({'row':job.row,'error':error,'uploaded_url':uploaded_url}))
            # If an upload succeeded, retain both local output and URL for manual reconciliation.
            if not uploaded_url:
                output.unlink(missing_ok=True)
            try:
                if marked:
                    sheet.update(job,'FAILED',output=uploaded_url,error=error)
            except Exception:
                log.exception('Could not update failed row %s; use manual recovery in README',job.row)
            self.update(failed=self.state.snapshot()['failed']+1,error=error)
