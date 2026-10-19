from dataclasses import dataclass
import logging
import gspread
from gspread.utils import rowcol_to_a1
from app.google_client import credentials, retry

log = logging.getLogger('video_builder')
HEADERS = ['ID','Voice Drive Link','Status','Output Link','Error']
STATUSES = {'NOT_PROCESSED','PROCESSING','COMPLETED','FAILED'}


@dataclass(frozen=True)
class Job:
    row: int
    id: str
    voice_link: str
    status: str


class Sheets:
    def __init__(self, config, stop=None):
        self.stop = stop
        client = gspread.authorize(credentials(config))
        client.set_timeout(30)
        book = retry(lambda:client.open_by_key(config['spreadsheet_id']),stop)
        self.sheet = retry(lambda:book.worksheet(config['worksheet_name']),stop)
        self.columns = {}
        log.info('Connected to Google Sheets')

    def jobs(self):
        rows = retry(self.sheet.get_all_values,self.stop)
        if not rows or any(rows[0].count(h)!=1 for h in HEADERS):
            raise ValueError('Sheet needs exactly one of each header: '+', '.join(HEADERS))
        self.columns = {h:rows[0].index(h)+1 for h in HEADERS}
        jobs = []
        for number, values in enumerate(rows[1:],2):
            if not any(values):
                continue
            def value(key):
                index = self.columns[key]-1
                return values[index].strip() if index<len(values) else ''
            status = value('Status')
            if status not in STATUSES:
                log.warning('Row %s has an unrecognized status and will be skipped',number)
            jobs.append(Job(number,value('ID'),value('Voice Drive Link'),status))
        return jobs

    def update(self, job, status, output='', error='', stop=None):
        # Atomic cell batch, actual worksheet row; RAW prevents formula injection.
        values = {'Status':status,'Output Link':output,'Error':error[:1000]}
        body = [{'range':rowcol_to_a1(job.row,self.columns[k]),'values':[[v]]} for k,v in values.items()]
        retry(lambda:self.sheet.batch_update(body,value_input_option='RAW'),stop)
