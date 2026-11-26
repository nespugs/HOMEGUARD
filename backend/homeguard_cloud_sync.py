
import os
import time
import database

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

TABLE_FIELDS = {
    "sensor_logs": [
        "record_uuid", "timestamp", "temperature",
        "humidity", "gas", "distance"
    ],
    "alert_logs": [
        "record_uuid", "timestamp", "alert_type", "message"
    ],
    "device_status_logs": [
        "record_uuid", "timestamp", "mode",
        "led", "fan", "buzzer"
    ],
    "sleep_logs": [
        "record_uuid", "sleep_start",
        "wake_time", "duration_seconds"
    ]
}

def synchronize(progress=None):
    uploaded = 0
    failed = 0
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")

    if not url or not key:
        raise RuntimeError("Missing Supabase credentials")

    client = create_client(url, key)
    database.init_db()

    # Start with one record per table for safe testing.
    batches = database.get_pending_sync_batch(limit=20)

    for table, records in batches.items():
        for record in records:
            uid = record["record_uuid"]

            # Avoid uploading an unfinished sleep session.
            if table == "sleep_logs" and record["wake_time"] is None:
                continue

            payload = {
                field: record[field]
                for field in TABLE_FIELDS[table]
            }

            if table == "device_status_logs":
                for field in ("led", "fan", "buzzer"):
                    payload[field] = bool(payload[field])

            try:
                # Upsert avoids duplicate UUID records.
                client.table(table).upsert(
                    payload,
                    on_conflict="record_uuid"
                ).execute()

                # Verify the record exists in Supabase.
                check = (
                    client.table(table)
                    .select("record_uuid")
                    .eq("record_uuid", uid)
                    .execute()
                )

                if not any(
                    row["record_uuid"] == uid
                    for row in check.data
                ):
                    raise RuntimeError(
                        "Cloud record could not be verified"
                    )

                database.mark_sync_confirmed(table, [uid])
                uploaded += 1
                if progress: progress(uploaded, failed, True, None)
                print(f"[SYNCED] {table}: {uid}")

            except Exception as error:
                database.record_sync_failure(
                    table, [uid], str(error)
                )
                failed += 1
                if progress: progress(uploaded, failed, False, error)
                print(
                    f"[PENDING] {table}: "
                    f"{type(error).__name__}: {error}"
                )

    print("\nSYNC STATUS")
    for table, counts in database.get_sync_counts().items():
        print(table, counts)
    return {"uploaded": uploaded, "failed": failed}


if __name__ == "__main__":
    synchronize()
