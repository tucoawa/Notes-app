import logging
import requests
from datetime import datetime
from extensions import db, scheduler
from models import Note
from flask import current_app


logging.getLogger('apscheduler.executors.default').setLevel(logging.ERROR)
logging.getLogger('apscheduler.scheduler').setLevel(logging.ERROR)


def check_due_notes(app):
    """Runs every minute: fire ntfy for notes whose due date has arrived.

    Only marks a note as notified when the notification actually succeeded
    (or when no URL is configured, in which case there is nothing to send).
    If the send fails, `notified` stays False, so the next minute's run
    will retry — up to a point. After MAX_ATTEMPTS consecutive failures we
    give up and mark as notified to avoid an infinite retry loop.
    """
    from models import Setting
    import requests

    MAX_ATTEMPTS = 10  # ~10 minutes of retries before giving up

    with app.app_context():
        now = datetime.utcnow()
        # Fetch notes that are due, but let Python filter out the ones
        # marked as done in their properties (since SQLite JSON querying
        # is tricky and properties can be NULL).
        due_raw = Note.query.filter(
            Note.due_at != None,                  # noqa: E711
            Note.due_at <= now,
            Note.notified == False,               # noqa: E712
        ).all()
        
        # Filter out notes that have a 'status' property set to 'done'
        due = []
        for note in due_raw:
            status = str((note.properties or {}).get('status', '')).strip().lower()
            # Handle cases where status might be comma-separated (e.g. "todo, done")
            statuses = [s.strip() for s in status.split(',')]
            if 'done' not in statuses:
                due.append(note)
        if not due:
            return

        url = Setting.get('ntfy_url', app.config['NTFY_URL'])

        # If no URL is configured, treat all due notes as handled — the
        # user has explicitly disabled notifications.
        if not url:
            for note in due:
                note.notified = True
            db.session.commit()
            return

        base_url = app.config.get('APP_BASE_URL', 'http://127.0.0.1:5000')

        for note in due:
            # Force a fresh read from the DB to ensure it wasn't snoozed.
            # If the row vanished (hard delete, DB reset, lock), skip it
            # rather than letting the whole tick crash.
            try:
                db.session.refresh(note)
            except Exception as e:
                app.logger.warning(
                    f"skipping note {note.id} during notifier refresh: {e}"
                )
                continue

            # If it was snoozed/done/cancelled while the scheduler was running, skip it
            if note.due_at is None or note.due_at > datetime.utcnow() or note.notified:
                continue

            succeeded = False
            try:
                click_url = f"{base_url}/?note={note.id}"
                action_url = f"{base_url}/note/{note.id}/mark-done"

                # ntfy requires headers to be ASCII/UTF-8 safe. 
                # We strip newlines from the title to prevent HTTP header errors.
                safe_title = (note.title or 'Note due').replace('\n', ' ').replace('\r', '')
                
                # The body is the note content (can be empty).
                body_text = note.content or ''

                # Use a zero-width space (\u200b) so ntfy doesn't insert "triggered"
                final_body = body_text.strip() if body_text and body_text.strip() else '\u200b'
                
                resp = requests.post(
                    url,
                    data=final_body.encode('utf-8'),
                    headers={
                        'Title': safe_title.encode('utf-8'),
                        'Priority': 'high',
                        'Tags': '',
                        'Click': click_url,
                        'Actions': f'http, OK, {action_url}, method=POST, clear=true; http, Snooze 30m, {base_url}/note/{note.id}/snooze, method=POST, clear=true; http, Cancel, {base_url}/note/{note.id}/cancel-reminder, method=POST, clear=true',
                        'Markdown': 'yes',
                    },
                    timeout=5,
                )
                # 2xx means ntfy accepted the message.
                if 200 <= resp.status_code < 300:
                    succeeded = True
                else:
                    app.logger.warning(
                        f"ntfy returned HTTP {resp.status_code} for note {note.id}: "
                        f"{resp.text[:200]}"
                    )
            except Exception as e:
                app.logger.warning(f"ntfy failed for note {note.id}: {e}")

            if succeeded:
                note.notified = True
            else:
                # Bump retry counter. If we've failed too many times,
                # mark as notified to stop trying — otherwise a persistent
                # ntfy outage would retry the same note forever.
                attempts = int(note.notify_attempts or 0) + 1
                note.notify_attempts = attempts
                if attempts >= MAX_ATTEMPTS:
                    app.logger.warning(
                        f"giving up on ntfy for note {note.id} after {attempts} attempts"
                    )
                    note.notified = True

        try:
            db.session.commit()
        except Exception as e:
            app.logger.warning(f"notifier commit failed: {e}")
            db.session.rollback()

def check_due_blocks(app):
    """Runs every minute: fire ntfy for block_meta rows whose due_at
    has arrived. Mirrors check_due_notes() but operates on block_meta.

    Notification shape:
      - title:  note's title
      - body:   the block's visible text
      - click:  opens the note
    """
    from models import Setting, BlockMeta, Note
    import block_utils

    MAX_ATTEMPTS = 10

    with app.app_context():
        now = datetime.utcnow()
        due = BlockMeta.query.filter(
            BlockMeta.due_at != None,           # noqa: E711
            BlockMeta.due_at <= now,
            BlockMeta.reminder_sent == False,   # noqa: E712
        ).all()
        if not due:
            return

        url = Setting.get('ntfy_url', app.config['NTFY_URL'])
        if not url:
            # No notifications configured — mark as sent so we don't retry
            for row in due:
                row.reminder_sent = True
            db.session.commit()
            return

        base_url = app.config.get('APP_BASE_URL', 'http://127.0.0.1:5000')

        for row in due:
            try:
                db.session.refresh(row)
            except Exception:
                continue

            if row.reminder_sent or row.due_at is None or row.due_at > datetime.utcnow():
                continue

            note = Note.query.get(row.note_id)
            if note is None or note.deleted_at is not None:
                # Note was deleted — drop the pending reminder
                row.reminder_sent = True
                continue

            # Find the block's text (from note content, by marker ID)
            blocks = block_utils.split_blocks(note.content or '')
            block_text = ''
            for b in blocks:
                if b['id'] == row.block_id:
                    block_text = b['text']
                    break
            if not block_text:
                # Block was edited away — mark sent to stop retrying
                row.reminder_sent = True
                continue

            succeeded = False
            try:
                click_url = f"{base_url}/?note={note.id}"
                safe_title = (note.title or 'Block reminder').replace('\n', ' ').replace('\r', '')
                body_text = block_text.strip() or '\u200b'

                resp = requests.post(
                    url,
                    data=body_text.encode('utf-8'),
                    headers={
                        'Title': safe_title.encode('utf-8'),
                        'Priority': 'high',
                        'Tags': '',
                        'Click': click_url,
                        'Actions': (
                            f'http, OK, {base_url}/note/{note.id}/block/{row.block_id}/mark-done, '
                            f'method=POST, clear=true; '
                            f'http, Snooze 30m, {base_url}/note/{note.id}/block/{row.block_id}/snooze, '
                            f'method=POST, clear=true; '
                            f'http, Cancel, {base_url}/note/{note.id}/block/{row.block_id}/cancel-reminder, '
                            f'method=POST, clear=true'
                        ),
                        'Markdown': 'yes',
                    },
                    timeout=5,
                )
                if 200 <= resp.status_code < 300:
                    succeeded = True
                else:
                    app.logger.warning(
                        f"ntfy block returned HTTP {resp.status_code} for "
                        f"note {note.id} block {row.block_id}: {resp.text[:200]}"
                    )
            except Exception as e:
                app.logger.warning(f"ntfy block failed for note {note.id}: {e}")

            if succeeded:
                row.reminder_sent = True
            else:
                # The BlockMeta table doesn't have a notify_attempts
                # column — use metadata_json as a counter.
                import json as _json
                try:
                    meta = _json.loads(row.metadata_json or '{}')
                except Exception:
                    meta = {}
                attempts = int(meta.get('_notify_attempts', 0)) + 1
                meta['_notify_attempts'] = attempts
                row.metadata_json = _json.dumps(meta)
                if attempts >= MAX_ATTEMPTS:
                    row.reminder_sent = True

        try:
            db.session.commit()
        except Exception as e:
            app.logger.warning(f"block notifier commit failed: {e}")
            db.session.rollback()            
            
def start_scheduler(app):
    if app.debug and not app.config.get('TESTING'):
        import os
        if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
            return

    scheduler.add_job(
        func=lambda: check_due_notes(app),
        trigger='interval',
        minutes=1,
        id='due_notes',
        replace_existing=True,
        misfire_grace_time=60,
        coalesce=True,
        max_instances=1,
    )
    
    scheduler.add_job(
        func=lambda: check_due_blocks(app),
        trigger='interval',
        minutes=1,
        id='due_blocks',
        replace_existing=True,
        misfire_grace_time=60,
        coalesce=True,
        max_instances=1,
    )    
    
    if not scheduler.running:
        scheduler.start()