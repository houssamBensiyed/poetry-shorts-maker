import asyncio
from app.config import load_config
from app.logger import configure
from app.processor import Processor
from app.dashboard import Dashboard
from textual.widgets import Button, Static, RichLog


def test_dashboard_opens_and_reports_configuration_error(tmp_path):
    async def scenario():
        c=load_config()
        logs=configure(tmp_path)
        p=Processor(c)
        app=Dashboard(p,logs)
        async with app.run_test(size=(80,30)) as pilot:
            await pilot.pause()
            assert app.query_one('#start',Button).disabled is False
            assert app.query_one('#logs',RichLog).lines
            await pilot.click('#start')
            await pilot.pause(.5)
            assert p.status()['status']=='error'
            assert 'spreadsheet_id' in p.status()['error']
            assert app.query_one('#start',Button).disabled is False
            await pilot.click('#exit')
        p.close()
    asyncio.run(scenario())
