import time
import uuid
from datetime import datetime
import database
from homeguard_cloud_sync import synchronize

SYNC_INTERVAL = 30
RUN_ID = str(uuid.uuid4())
database.init_db()
print("================================")
print("HOMEGUARD AUTOMATIC CLOUD SYNC")
print("================================")
print(f"Interval: {SYNC_INTERVAL} seconds")
print("Press CTRL+C to stop.\n")

def progress(uploaded, failed, success, error):
    database.update_sync_health(
        RUN_ID, status='syncing', success=success,
        error=str(error) if error else None,
        uploaded=uploaded, failed=failed
    )

try:
    while True:
        print("\nSync started:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        database.update_sync_health(RUN_ID, status='syncing', cycle_start=True)
        try:
            result = synchronize(progress=progress)
            uploaded, failed = result['uploaded'], result['failed']
            status = 'error' if failed else 'healthy'
            if failed and uploaded:
                status = 'partial_error'
            database.update_sync_health(RUN_ID, status=status,
                                        cycle_finish=True, uploaded=uploaded, failed=failed)
        except Exception as error:
            database.update_sync_health(RUN_ID, status='error', cycle_finish=True,
                                        error=f'{type(error).__name__}: {error}')
            print('[SYNC ERROR]', type(error).__name__, str(error))
        print(f"Next sync in {SYNC_INTERVAL} seconds...")
        # Refresh heartbeat while idle; otherwise a long sleep or large batch
        # could make the dashboard incorrectly label the uploader inactive.
        for _ in range(SYNC_INTERVAL):
            time.sleep(1)
            database.update_sync_health(RUN_ID)
except KeyboardInterrupt:
    database.update_sync_health(RUN_ID, status='stopped')
    print("\nHOMEGUARD automatic sync stopped.")
