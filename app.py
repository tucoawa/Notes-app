import unicodedata
import os
import re
import json
import shutil
import uuid
import zipfile
import io
from datetime import datetime, timezone
from sqlalchemy.orm import selectinload
from urllib.parse import urlencode
from werkzeug.utils import secure_filename
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, send_from_directory, make_response, send_file)
from flask_compress import Compress                   
from flask_login import login_user, logout_user, current_user
from slugify import slugify

from config import Config
from extensions import db, login_manager
from models import Note, Attachment, SavedSearch, Setting, User, Capture, BlockMeta
import block_utils
from notifier import start_scheduler
from PIL import Image, ImageOps


class VirtualNote:
    def __init__(self, id, title, is_custom=False):
        self.id = id
        self.title = title
        self.has_children = True
        self.is_template = False
        self.is_map_note = False
        self.is_custom = is_custom
        self.content = ''
        self.sort_by = 'modified'
        self.sort_dir = 'asc'
        self.due_at = None
        self.properties = {}
        self.children = []
        self.parent_id = None
        self.deleted_at = None
        # Add dates so the sorters don't crash if they try to sort by date
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

def _note_title_sort_key(n, reverse=False):
    """Shared natural-sort key for note titles.

    - Accent-strips and lowercases.
    - Buckets numeric-leading titles separately from letter-leading ones so
      "2024 Taxes" and "Apple" don't interleave.
    - Splits on digit runs and converts them to ints ("Note 2" < "Note 10").
    - When `reverse` is True, flips the numeric/letter bucket so letter-titles
      sink to the bottom in descending order.
    """
    t = (n.title or '').lower()
    t = ''.join(
        c for c in unicodedata.normalize('NFD', t)
        if unicodedata.category(c) != 'Mn'
    )
    starts_with_digit = bool(t) and t[0].isdigit()
    if reverse:
        bucket = 1 if starts_with_digit else 0
    else:
        bucket = 0 if starts_with_digit else 1
    parts = [int(text) if text.isdigit() else text for text in re.split(r'(\d+)', t)]
    return (bucket, parts)


# ─────────────────────────────────────────────────────────────
# CACHES
# Cached in app.config to avoid scanning 2000+ notes on every
# page load.
# ─────────────────────────────────────────────────────────────

def invalidate_parent_notes_cache():
    from flask import current_app, has_app_context
    import time
    if has_app_context():
        current_app.config['_parent_notes_cache_data'] = None
        current_app.config['_sidebar_cache'] = None
        # Add a timestamp to force the sidebar cache key to change on every delete/save
        current_app.config['_cache_timestamp'] = str(time.time())

def get_cached_sidebar_html(app, context):
    """Cache the rendered sidebar HTML to avoid rebuilding 727 Jinja nodes on every request."""
    from flask import current_app, render_template
    
    # Create a cache key based on the sort and search params.
    # If any of these change, the cache rebuilds.
    cache_key = (
        context.get('root_sort_by'),
        context.get('root_sort_dir'),
        context.get('query_suffix'),
        context.get('current_search_id'),
        context.get('current').id if context.get('current') else None,
        current_app.config.get('_cache_timestamp', ''), # <--- ADD THIS LINE
    )
    
    cached = current_app.config.get('_sidebar_cache')
    if cached and cached.get('key') == cache_key:
        return cached['html']

    # Render the sidebar template standalone
    html = render_template('_sidebar.html', **context)
    current_app.config['_sidebar_cache'] = {'key': cache_key, 'html': html}
    return html        

def get_parent_notes():
    from flask import current_app, has_app_context
    if not has_app_context():
        return []
    cached = current_app.config.get('_parent_notes_cache_data')
    if cached is not None:
        return cached

    # P5: direct join is faster than `Note.children.any()` (correlated EXISTS).
    # Use the existing ix_note_parent_id index to get distinct parent ids,
    # then fetch only those notes.
    parent_ids_q = (
        db.session.query(Note.parent_id)
        .filter(
            Note.parent_id.isnot(None),
            Note.deleted_at.is_(None),
        )
        .distinct()
    )
    result = (
        Note.query
        .filter(Note.id.in_(parent_ids_q))
        .order_by(Note.title)
        .all()
    )
    current_app.config['_parent_notes_cache_data'] = result
    return result


def invalidate_prop_cache():
    """Invalidate the cached property values. Call this when notes change."""
    from flask import current_app, has_app_context
    if has_app_context():
        current_app.config['_prop_cache_data'] = None
        current_app.config['_dashboard_scan_cache'] = None
        current_app.config['_qs_index'] = None


def get_dashboard_scan():
    """Single-scan cache for dashboard data.

    Walks all live notes exactly once and builds:
      - available_priorities: sorted unique `priority` values across all notes
      - available_urgencies:  sorted unique `urgency` values across all notes
      - todo_tasks:           list of notes whose `category` includes
                              "tasks", are not done/canceled, have a
                              priority, and have no due date.

    Cached in `_dashboard_scan_cache`; invalidated by `invalidate_prop_cache()`.
    """
    from flask import current_app, has_app_context
    if not has_app_context():
        return {'available_priorities': [], 'available_urgencies': [], 'todo_tasks': []}

    cached = current_app.config.get('_dashboard_scan_cache')
    if cached is not None:
        return cached

    all_notes = Note.query.filter(Note.deleted_at.is_(None)).all()

    def _norm(v):
        s = str(v).strip() if v is not None else ''
        if s.endswith('.0'):
            s = s[:-2]
        return s

    prio_set = set()
    urg_set = set()
    todo = []

    for n in all_notes:
        props = n.properties or {}
        try:
            prio = _norm(props.get('priority'))
            urg = _norm(props.get('urgency'))

            if prio:
                prio_set.add(prio)
            if urg:
                urg_set.add(urg)

            category = str(props.get('category', '')).lower()
            if 'tasks' not in [c.strip() for c in category.split(',')]:
                continue

            status = str(props.get('status', '')).lower().strip()
            tags = str(props.get('tags', '')).lower().strip()
            if 'done' in status:
                continue
            if 'canceled' in status or 'cancelled' in status:
                continue
            if 'canceled' in tags or 'cancelled' in tags:
                continue

            if not prio:
                continue

            due = str(props.get('due', '')).strip()
            if due:
                continue

            n._priority_val = prio
            n._urgency = urg
            todo.append(n)
        except Exception as e:
            print(f"[dashboard_scan] skipped note {n.id}: {e}")

    def _prio_key(p):
        try:
            return (0, float(p))
        except ValueError:
            return (1, p)

    result = {
        'available_priorities': sorted(prio_set, key=_prio_key),
        'available_urgencies': sorted(urg_set, key=_prio_key),
        'todo_tasks': todo,
    }
    current_app.config['_dashboard_scan_cache'] = result
    return result

def get_quick_switcher_index():
    """Return a cached list of all live notes as lightweight dicts.

    Used by /api/quick-switcher to avoid a DB round-trip per keystroke.
    Each entry is { id, title, title_norm, updated_at_iso, updated_at_ts }.

    Invalidated by invalidate_prop_cache() after any note save/delete/move.
    """
    from flask import current_app, has_app_context
    if not has_app_context():
        return []
    cached = current_app.config.get('_qs_index')
    if cached is not None:
        return cached

    rows = Note.query.filter(Note.deleted_at.is_(None)).with_entities(
        Note.id, Note.title, Note.updated_at
    ).all()

    def _norm(s):
        if not s:
            return ''
        return ''.join(
            c for c in unicodedata.normalize('NFD', str(s))
            if unicodedata.category(c) != 'Mn'
        ).lower()

    index = []
    for nid, title, updated in rows:
        ts = updated.timestamp() if updated else 0.0
        index.append({
            'id': nid,
            'title': title or '',
            'title_norm': _norm(title or ''),
            'updated_at_ts': ts,
        })
    current_app.config['_qs_index'] = index
    return index    

def get_collect_prop_values():
    from flask import current_app, has_app_context
    from sqlalchemy import text

    if not has_app_context():
        return {}
    cached = current_app.config.get('_prop_cache_data')
    if cached is not None:
        return cached

    prop_values = {}
    try:
        # Let SQLite do the JSON parsing
        rows = db.session.execute(text("""
            SELECT LOWER(TRIM(j.key)) AS k, j.value AS v
            FROM note, json_each(note.properties) AS j
            WHERE j.key IS NOT NULL AND j.key != ''
              AND j.value IS NOT NULL
        """)).all()
        for k, v in rows:
            if not k:
                continue
            for piece in str(v).split(','):
                piece = piece.strip().lower()
                if piece:
                    prop_values.setdefault(k, set()).add(piece)
    except Exception as e:
        print(f"[CACHE] SQLite JSON failed ({e}), falling back to Python")
        for n in Note.query.all():
            for k, v in (n.properties or {}).items():
                if not k:
                    continue
                key = str(k).strip().lower()
                for piece in str(v).split(','):
                    piece = piece.strip().lower()
                    if piece:
                        prop_values.setdefault(key, set()).add(piece)

    result = {k: sorted(vs) for k, vs in prop_values.items()}
    current_app.config['_prop_cache_data'] = result
    return result


def resize_image_if_needed(file_mimetype, file_path):
    """Shrink an uploaded image in place according to
    Setting('image_resize_percent').

    Does nothing for non-image files, or when the percentage is 100
    or outside the 10–100 range.
    """
    if not (file_mimetype or '').startswith('image/'):
        return

    try:
        percent = int(Setting.get('image_resize_percent', '100'))
    except (ValueError, TypeError):
        percent = 100

    if percent >= 100 or percent < 10:
        return

    try:
        img = Image.open(file_path)
        img = ImageOps.exif_transpose(img)  # fix sideways phone photos
        w, h = img.size
        new_w = max(10, int(w * percent / 100))
        new_h = max(10, int(h * percent / 100))
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        fmt = img.format or 'JPEG'
        resized.save(file_path, format=fmt, optimize=True, quality=85)
    except Exception as e:
        print(f"[resize_image_if_needed] failed for {file_path}: {e}")


def ensure_column(table, column, coltype):
    """Add a column to `table` if it doesn't exist. Idempotent."""
    from sqlalchemy import text
    conn = db.engine.connect()
    try:
        existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        if column not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
            conn.commit()
            print(f"[migration] added {table}.{column}")
    finally:
        conn.close()


def create_app():
    app = Flask(__name__)
    app.config['PROPAGATE_EXCEPTIONS'] = True    
    app.config.from_object(Config)
    if not app.config['UPLOAD_FOLDER'].startswith('/data/data'):
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    Compress(app)

    @app.before_request
    def extend_timeout_for_uploads():
        if request.method == 'POST' and request.path.endswith('/upload'):
            request.environ['werkzeug.socket_timeout'] = 300

    with app.app_context():
        # P7: SQLite tuning PRAGMAs. Registered on the engine's "connect"
        # event so it runs once per physical connection. Must be inside the
        # app context because db.engine is only reachable through current_app.
        #
        # - WAL: better concurrency (the notifier writes while the web threads read).
        # - synchronous=NORMAL: much faster commits, still safe enough for a notes app.
        # - temp_store=MEMORY: keep sorts/joins in RAM.
        # - cache_size=-64000: 64 MB page cache (negative value = KiB).
        #
        # ⚠  WAL mode creates notes.db-wal and notes.db-shm alongside notes.db.
        #    If your backup only copies notes.db, back up the -wal file too, or
        #    run `PRAGMA wal_checkpoint(TRUNCATE);` before copying.
        from sqlalchemy import event
        if not getattr(db.engine, "_pragmas_registered", False):
            @event.listens_for(db.engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL;")
                    cur.execute("PRAGMA synchronous=NORMAL;")
                    cur.execute("PRAGMA temp_store=MEMORY;")
                    cur.execute("PRAGMA cache_size=-64000;")
                finally:
                    cur.close()

                # Register custom SQLite functions on EVERY new connection
                def strip_accents(text):
                    if not text:
                        return text
                    import unicodedata
                    return ''.join(c for c in unicodedata.normalize('NFD', str(text)) if unicodedata.category(c) != 'Mn').lower()

                def natural_sort_key(text):
                    if not text:
                        return ''
                    import re
                    t = str(text).lower()
                    return re.sub(r'\d+', lambda m: m.group(0).zfill(10), t)

                dbapi_conn.create_function("STRIP_ACCENTS", 1, strip_accents)
                dbapi_conn.create_function("NATURAL_SORT", 1, natural_sort_key)

            db.engine._pragmas_registered = True

        db.create_all()

        # Sweep orphaned chunk upload directories older than 24 hours
        try:
            import time as _time
            chunk_root = os.path.join(app.instance_path, 'chunk_uploads')
            if os.path.isdir(chunk_root):
                cutoff = _time.time() - 24 * 3600
                for name in os.listdir(chunk_root):
                    p = os.path.join(chunk_root, name)
                    try:
                        if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                            import shutil as _shutil
                            _shutil.rmtree(p)
                    except OSError:
                        pass
        except Exception as e:
            print(f"[chunk cleanup] {e}")        
        
        # --- schema migrations (idempotent) ---
        ensure_column('saved_search', 'view_mode', "VARCHAR(20) DEFAULT 'list'")
        ensure_column('note', 'notify_attempts', "INTEGER DEFAULT 0")
        ensure_column('note', 'deleted_at', "DATETIME") # <--- ADD THIS
        ensure_column('note', 'is_map_note', "BOOLEAN DEFAULT 0")
        # --- schema migrations (idempotent) ---
        ensure_column('saved_search', 'view_mode', "VARCHAR(20) DEFAULT 'list'")
        ensure_column('note', 'notify_attempts', "INTEGER DEFAULT 0")
        ensure_column('note', 'deleted_at', "DATETIME") # <--- ADD THIS
        ensure_column('note', 'is_map_note', "BOOLEAN DEFAULT 0")        
        # Add indexes for performance on large databases
        from sqlalchemy import text
        with db.engine.connect() as conn:
            try:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_note_title ON note (title);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_note_parent_id ON note (parent_id);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_note_updated_at ON note (updated_at);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_note_created_at ON note (created_at);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_note_due_at ON note (due_at);"))
                # P1: indexes for the hottest filters
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_note_deleted_at ON note (deleted_at);"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_note_parent_deleted ON note (parent_id, deleted_at);"))
                conn.commit()
                print("[migration] ensured performance indexes")
            except Exception as e:
                print(f"Index creation skipped: {e}")
        # --------------------------------------
        seed_user(app)
        
        # Clean up block_meta rows whose marker is no longer in the
        # note's content. Runs once at startup. Safe: only deletes
        # rows where the block_id is definitively gone.
        try:
            _notes = db.session.query(Note.id, Note.content).all()
            _all_markers = set()
            for _note_id, _content in _notes:
                for _mid in block_utils.markers_in(_content or ''):
                    _all_markers.add((_note_id, _mid))
            _cleaned = 0
            for _row in BlockMeta.query.all():
                if (_row.note_id, _row.block_id) not in _all_markers:
                    db.session.delete(_row)
                    _cleaned += 1
            db.session.commit()
            if _cleaned:
                print(f"[orphan cleanup] removed {_cleaned} orphan block_meta rows")
        except Exception as _e:
            print(f"[orphan cleanup] skipped: {_e}")
            db.session.rollback()        
        
        # Pre-warm the property-value cache
        try:
            get_collect_prop_values()
        except Exception as e:
            print(f"[cache prewarm] skipped: {e}")

        # Pre-warm the quick-switcher index so the first @-autocomplete
        # doesn't pay the full DB-scan cost.
        try:
            get_quick_switcher_index()
        except Exception as e:
            print(f"[qs_index prewarm] skipped: {e}")

    register_routes(app)
    register_filters(app)
    start_scheduler(app)
    return app


def seed_user(app):
    username = app.config['APP_USERNAME']
    password = app.config['APP_PASSWORD']
    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()


def bootstrap_root(app):
    """Ensure a real Root note exists, and reparent any top-level notes
    under it. Idempotent: safe to run on every startup.
    """
    from models import Setting

    root = None
    stored_id = Setting.get('root_note_id', '').strip()
    if stored_id.isdigit():
        root = Note.query.get(int(stored_id))

    if root is None:
        root = Note.query.filter(
            Note.parent_id.is_(None),
            db.or_(Note.title == 'Root', Note.slug.like('root-%')),
        ).first()

    if root is None:
        root = Note(title='Root', slug='root', content='',
                    parent_id=None, is_template=False, properties={})
        db.session.add(root)
        db.session.commit()
        root.slug = f"root-{root.id}"
        db.session.commit()

    if Setting.get('root_note_id', '') != str(root.id):
        Setting.set('root_note_id', str(root.id))

    orphans = Note.query.filter(
        Note.parent_id.is_(None),
        Note.id != root.id,
    ).all()
    for n in orphans:
        n.parent_id = root.id
    if orphans:
        db.session.commit()


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def _sorted_children(children, sort_by='title', sort_dir='asc'):
    children = list(children)
    reverse = (sort_dir == 'desc')

    parents_first = False
    if sort_by.endswith('_pf'):
        parents_first = True
        sort_by = sort_by[:-3]
        
    if sort_by == 'title':
        children.sort(key=lambda n: _note_title_sort_key(n, reverse=reverse), reverse=reverse)
    elif sort_by == 'created':
        children.sort(key=lambda n: n.created_at or datetime.min, reverse=reverse)
    elif sort_by == 'modified':
        children.sort(key=lambda n: n.updated_at or datetime.min, reverse=reverse)
    elif sort_by == 'due':
        children.sort(key=lambda n: (n.due_at is None, n.due_at or datetime.max), reverse=reverse)
    else:
        def prop_sort_key(n):
            val = (n.properties or {}).get(sort_by, '')
            val_str = str(val).strip().lower()
            if not val_str:
                return (1, 0, '') if not reverse else (-1, 0, '')
            try:
                num = float(val_str)
                return (0, num, '')
            except ValueError:
                return (0, 0, val_str)
        children.sort(key=prop_sort_key, reverse=reverse)

    if parents_first:
        def _is_parent(n):
            if hasattr(n, 'has_children'):
                return n.has_children
            return bool(n.children)
        children.sort(key=lambda n: 0 if _is_parent(n) else 1)

    return children


def _child_preview(text, limit=140):
    if not text:
        return ''
    stripped_lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r'^!\[[^\]]*\]\([^)]+\)\s*$', line):
            continue
        line = re.sub(r'^#{1,6}\s+', '', line)
        line = re.sub(r'^[-*+]\s+', '', line)
        line = re.sub(r'^\d+\.\s+', '', line)
        line = re.sub(r'^>\s+', '', line)
        line = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', line)
        line = re.sub(r'\[([^\]]*)\]\([^)]+\)', r'\1', line)
        line = re.sub(r'(\*\*|__|\*|_|`)', '', line)
        line = re.sub(r'(?:@)?\[\[([^\]]+)\]\]', r'\1', line)
        line = line.strip()
        if line:
            stripped_lines.append(line)
            break
    out = stripped_lines[0] if stripped_lines else ''
    if len(out) > limit:
        out = out[:limit].rstrip() + '…'
    return out

        
def register_filters(app):
    @app.template_filter('sort_children')
    def sort_children(children, sort_by='title', sort_dir='asc'):
        return _sorted_children(children, sort_by, sort_dir)

    @app.template_filter('child_preview')
    def child_preview(text, limit=140):
        return _child_preview(text, limit)
        
    @app.template_filter('markdown')
    def markdown_filter(text):
        if not text:
            return ''
        import markdown
        import re
        
        # 1. Pre-process task lists BEFORE parsing
        def convert_task_list(match):
            indent = match.group(1)
            is_checked = match.group(2).lower() in ('x', 'checked')
            task_text = match.group(3)
            checkbox = f'<input type="checkbox" {"checked" if is_checked else ""} disabled>'
            li_class = 'task-list-item checked' if is_checked else 'task-list-item'
            return f'{indent}<li class="{li_class}">{checkbox} {task_text}</li>'
            
        # Convert markdown task list items to raw HTML
        processed_text = re.sub(
            r'^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$', 
            convert_task_list, 
            text, 
            flags=re.MULTILINE
        )
        
        # 2. Parse the rest of the markdown
        html = markdown.markdown(processed_text, extensions=['fenced_code', 'tables', 'nl2br'])
        
        return html     


def _split_prop_values(values):
    out = set()
    for v in values or []:
        for p in str(v).split(','):
            p = p.strip().lower()
            if p:
                out.add(p)
    return sorted(out)


def _get_journal_parent():
    """Return the Journal parent note, creating it if missing."""
    stored = Setting.get('journal_parent_id', '').strip()
    if stored.isdigit():
        n = Note.query.get(int(stored))
        if n:
            return n
    root = None
    stored_root = Setting.get('root_note_id', '').strip()
    if stored_root.isdigit():
        root = Note.query.get(int(stored_root))
    j = None
    if root:
        j = Note.query.filter_by(parent_id=root.id, title='Journal').first()
    if not j:
        j = Note(title='Journal', slug='journal', content='',
                 parent_id=root.id if root else None,
                 is_template=False, properties={})
        db.session.add(j)
        db.session.commit()
        j.slug = f"journal-{j.id}"
        db.session.commit()
    Setting.set('journal_parent_id', str(j.id))
    return j


def substitute_vars(text):
    if not isinstance(text, str):
        return text
    now = datetime.now()
    mapping = {
        'date':     now.strftime('%Y-%m-%d'),
        'time':     now.strftime('%H:%M'),
        'datetime': now.strftime('%Y-%m-%d %H:%M'),
        'day':      now.strftime('%A'),
        'year':     now.strftime('%Y'),
        'month':    now.strftime('%m'),
        'week':     now.strftime('%V'),
    }
    def repl(m):
        key = m.group(1).strip().lower()
        return mapping.get(key, m.group(0))
    return re.sub(r'\{\{\s*([a-z_]+)\s*\}\}', repl, text)


def _apply_text_template(prefix, suffix, text):
    out = text
    p = prefix or ''
    s = suffix or ''
    
    if p:
        # If the prefix already ends with a newline, don't add another one.
        # Otherwise, add exactly one.
        if not p.endswith('\n'):
            p = p + '\n'
        out = p + out
        
    if s:
        if not out.endswith('\n'):
            out = out + '\n'
        out = out + s
        
    return out

def strip_accents(s):
    if not s:
        return ''
    return ''.join(c for c in unicodedata.normalize('NFD', str(s))
                   if unicodedata.category(c) != 'Mn').lower()    

def fuzzy_score(query, title):
    q = strip_accents(query).strip()
    t = strip_accents(title)
    if not q:
        return 0

    # Tier 1: prefix match
    if t.startswith(q):
        return 1000 - len(t)

    # Tier 2: word-boundary match
    words = t.split()
    for i, w in enumerate(words):
        if w.startswith(q):
            return 800 - i * 10 - len(t)

    # Tier 3: substring match
    idx = t.find(q)
    if idx >= 0:
        return 600 - idx - len(t)

    # Tier 4: character-subsequence match.
    # This is deliberately narrow:
    #   (a) only allowed for short queries (<= 3 chars), so
    #       "quilombola" can't scattered-match a 180-char title;
    #   (b) the matched characters must be reasonably dense
    #       (gaps <= 3 * query length), so long titles can't
    #       "match" by having the query's letters sprinkled
    #       throughout their length.
    if len(q) > 3:
        return 0

    qi = 0
    gaps = 0
    last_match = -1
    for ti, ch in enumerate(t):
        if qi < len(q) and ch == q[qi]:
            if last_match >= 0:
                gaps += ti - last_match - 1
            last_match = ti
            qi += 1

    if qi != len(q):
        return 0
    if gaps > len(q) * 3:
        return 0
    return max(1, 400 - gaps - len(t))


def sort_notes(notes, sort_by, sort_dir):
    reverse = (sort_dir == 'desc')

    if sort_by == 'title':
        notes.sort(key=lambda n: _note_title_sort_key(n, reverse=reverse), reverse=reverse)
    elif sort_by == 'created':
        notes.sort(key=lambda n: n.created_at or datetime.min, reverse=reverse)
    elif sort_by == 'due':
        notes.sort(key=lambda n: (n.due_at is None, n.due_at or datetime.max), reverse=reverse)
    elif sort_by == 'parents_first':
        notes.sort(key=lambda n: (0 if n.children else 1, (n.title or '').lower()), reverse=False)
    elif sort_by in ('modified', 'updated'):
        notes.sort(key=lambda n: n.updated_at or datetime.min, reverse=reverse)
    else:
        def prop_sort_key(n):
            val = (n.properties or {}).get(sort_by, '')
            val_str = str(val).strip().lower()
            if not val_str:
                return (1, 0, '') if not reverse else (-1, 0, '')
            try:
                num = float(val_str)
                return (0, num, '')
            except ValueError:
                return (0, 0, val_str)
        notes.sort(key=prop_sort_key, reverse=reverse)

    return notes


# ─────────────────────────────────────────────────────────────
# Root helpers
# ─────────────────────────────────────────────────────────────

def get_root_note():
    """Return the Root note row.

    Deliberately does NOT eager-load children/grandchildren: Root has
    hundreds of direct children in a typical imported vault, and
    hydrating them all on every request is the single biggest cost in
    index(). Callers that need children fetch them explicitly with
    their own LIMIT.
    """
    stored_id = Setting.get('root_note_id', '').strip()
    if stored_id.isdigit():
        n = Note.query.get(int(stored_id))
        if n:
            return n
    return Note.query.filter(
        Note.parent_id.is_(None),
        db.or_(Note.title == 'Root', Note.slug.like('root-%')),
    ).first()


TOP_LEVEL_RENDER = 10000


def get_top_level_children(root, sort_by='modified', sort_dir='desc'):
    from sqlalchemy import desc, asc, func

    if root is None:
        return [], 0

    total_count = db.session.query(func.count(Note.id)).filter(
        Note.parent_id == root.id,
        Note.deleted_at.is_(None)
    ).scalar() or 0

    reverse = (sort_dir == 'desc')
    parents_first = False
    sb = sort_by or 'modified'
    if sb.endswith('_pf'):
        parents_first = True
        sb = sb[:-3]

    def apply_sort(q):
        if sb == 'title':
            return q.order_by(Note.id)
        if sb == 'due':
            if reverse:
                return q.order_by(Note.due_at.is_(None).asc(), Note.due_at.desc(), Note.id)
            return q.order_by(Note.due_at.is_(None).asc(), Note.due_at.asc(), Note.id)
        # ADD THIS BLOCK
        if sb == 'created':
            order = desc(Note.created_at) if reverse else asc(Note.created_at)
            return q.order_by(order, Note.id)
        # -----------------
        if sb in ('modified', 'updated', ''):
            order = desc(Note.updated_at) if reverse else asc(Note.updated_at)
            return q.order_by(order, Note.id)
        return q.order_by(desc(Note.updated_at), Note.id)

    if parents_first:
        direct_ids = {
            row[0] for row in
            db.session.query(Note.id)
            .filter(Note.parent_id == root.id, Note.deleted_at.is_(None))
            .all()
        }

        folder_ids = {
            row[0] for row in
            db.session.query(Note.parent_id)
            .filter(Note.parent_id.in_(direct_ids), Note.deleted_at.is_(None))
            .distinct()
            .all()
        }

        folders = []
        if folder_ids:
            fq = Note.query.filter(Note.id.in_(folder_ids), Note.deleted_at.is_(None))
            fq = apply_sort(fq)
            folders = fq.all()

        remaining_slots = max(0, TOP_LEVEL_RENDER - len(folders))
        if remaining_slots:
            nq = Note.query.filter(Note.parent_id == root.id, Note.deleted_at.is_(None))
            if folder_ids:
                nq = nq.filter(Note.id.notin_(folder_ids))
            nq = apply_sort(nq)
            non_folders = nq.limit(remaining_slots).all()
        else:
            non_folders = []

        rows = folders + non_folders
    else:
        q = Note.query.filter(Note.parent_id == root.id, Note.deleted_at.is_(None))
        q = apply_sort(q)
        rows = q.limit(TOP_LEVEL_RENDER).all()

    # (title sort is applied later, after has_children is known)
    if sb not in ('title', 'created', 'due', 'modified', 'updated', ''):
        rows = _sorted_children(rows, sort_by, sort_dir)

    # 1. Calculate has_children FIRST so the sorter knows the truth
    ids = [n.id for n in rows]
    if ids:
        parent_ids = {
            row[0] for row in db.session.query(Note.parent_id)
            .filter(Note.parent_id.in_(ids), Note.deleted_at.is_(None))
            .distinct()
            .all()
        }
    else:
        parent_ids = set()
    for n in rows:
        n.has_children = n.id in parent_ids

    # 2. NOW sort them (title sort uses the same helper as elsewhere)
    if sb == 'title':
        rows.sort(key=lambda n: _note_title_sort_key(n, reverse=reverse), reverse=reverse)

    # Apply folders-first sorting if requested
    if parents_first:
        def _is_parent(n):
            return getattr(n, 'has_children', False)
        rows.sort(key=lambda n: 0 if _is_parent(n) else 1)

    return rows, total_count


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

def register_routes(app):
    # Make the Setting model available to all Jinja templates
    @app.context_processor
    def inject_globals():
        return dict(Setting=Setting)	
    @app.teardown_appcontext
    def shutdown_session(exception=None):
        db.session.remove()

    SORT_OPTIONS = {
        'title', 'created', 'modified', 'due',
        'title_pf', 'created_pf', 'modified_pf', 'due_pf',
    }
    DIR_OPTIONS = {'asc', 'desc'}
    DUE_OPS = {'<', '<=', '=', '>=', '>'}

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user, remember=True)
                next_url = request.args.get('next') or url_for('index')
                return redirect(next_url)
            error = 'Invalid credentials'
        return render_template('login.html', error=error)

    @app.route('/logout')
    def logout():
        logout_user()
        return redirect(url_for('login'))

    @app.after_request
    def no_cache_html(response):
        ctype = response.headers.get('Content-Type', '')
        if 'text/html' in ctype:
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0, private'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '-1'
        return response

    @app.before_request
    def require_login():
        allowed = {
            'login', 'static', 'service_worker',
            'note_mark_done', 'note_snooze', 'note_cancel_reminder',
            'block_mark_done', 'block_snooze', 'block_cancel_reminder',
            'api_note_last_change',
        }
        if request.endpoint in allowed:
            return None
        if not current_user.is_authenticated:
            return redirect(url_for('login', next=request.full_path))

    @app.route('/note/<int:note_id>/mark-done', methods=['GET', 'POST'])
    def note_mark_done(note_id):
        note = Note.query.get_or_404(note_id)

        props = dict(note.properties or {})
        props['status'] = 'done'
        note.properties = sort_props_status_last(props)

        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(note, "properties")

        note.due_at = None
        note.notified = True

        db.session.commit()

        # The note's `status` property just changed to "done". If this is
        # the first note ever marked done, the property-value cache and
        # the cached sidebar HTML still hold the old status list. Invalidate
        # both so the advanced-search Status dropdown picks up "done" on
        # the next render. (note_snooze and note_cancel_reminder already do
        # the same thing — this keeps all three consistent.)
        invalidate_prop_cache()
        invalidate_parent_notes_cache()

        import time
        Setting.set(f'last_change_{note_id}', str(int(time.time() * 1000)))
        Setting.set(f'last_change_client_{note_id}', 'external')

        return jsonify({'ok': True}), 200
        
    @app.route('/note/<int:note_id>/cancel-reminder', methods=['GET', 'POST'])
    def note_cancel_reminder(note_id):
        note = Note.query.get_or_404(note_id)

        # Remove the active reminder but keep the due/time properties
        note.due_at = None
        note.notified = True
        note.notify_attempts = 0

        db.session.commit()

        invalidate_prop_cache()
        invalidate_parent_notes_cache()

        import time
        Setting.set(f'last_change_{note_id}', str(int(time.time() * 1000)))
        Setting.set(f'last_change_client_{note_id}', 'external')

        return jsonify({'ok': True}), 200
        
    @app.route('/note/<int:note_id>/snooze', methods=['GET', 'POST'])
    def note_snooze(note_id):
        from datetime import timedelta
        from sqlalchemy.orm.attributes import flag_modified
        import time
        
        note = Note.query.get_or_404(note_id)

        # 1. CRITICAL: Mark as notified and commit IMMEDIATELY.
        # This tells the background scheduler to skip this note on its next tick.
        note.notified = True
        note.notify_attempts = 0
        db.session.commit()

        # 2. Now calculate the new time
        if note.due_at:
            base_time = note.due_at
        else:
            base_time = datetime.utcnow()
            
        new_due = base_time + timedelta(minutes=30)
        local_due = datetime.now() + timedelta(minutes=30)
        
        props = dict(note.properties or {})
        props['due'] = local_due.strftime('%Y-%m-%d')
        props['time'] = local_due.strftime('%H:%M')
        
        note.properties = sort_props_status_last(props)
        flag_modified(note, 'properties')
        
        note.due_at = new_due
        # 3. Set notified back to False so it CAN fire in 30 mins
        note.notified = False
        
        db.session.commit()
        
        invalidate_prop_cache()
        invalidate_parent_notes_cache()

        Setting.set(f'last_change_{note_id}', str(int(time.time() * 1000)))
        Setting.set(f'last_change_client_{note_id}', 'external')

        return jsonify({'ok': True, 'new_due_at': new_due.isoformat() + 'Z'}), 200

    @app.route('/api/note/<int:note_id>/last-change')
    def api_note_last_change(note_id):
        v = Setting.get(f'last_change_{note_id}', '0')
        c = Setting.get(f'last_change_client_{note_id}', '')
        return jsonify({
            'ts': int(v) if v.isdigit() else 0,
            'client': c,
        })

    @app.route('/export/markdown')
    def export_markdown():
        root = get_root_note()
        if not root:
            return "No notes to export", 404

        memory_file = io.BytesIO()
        used_paths = set()

        def sanitize_filename(name):
            name = re.sub(r'[\\/*?:"<>|]', "", name).strip()
            return name or "Untitled"

        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:

            def process_note(note, current_path=""):
                safe_title = sanitize_filename(note.title)

                is_folder = len(note.children) > 0

                if is_folder:
                    folder_path = os.path.join(current_path, safe_title).replace("\\", "/")
                    full_path = os.path.join(folder_path, "index.md").replace("\\", "/")
                    next_parent_path = folder_path
                else:
                    full_path = os.path.join(current_path, f"{safe_title}.md").replace("\\", "/")
                    next_parent_path = current_path

                counter = 1
                base_path = full_path
                while full_path in used_paths:
                    if is_folder:
                        full_path = base_path.replace("index.md", f"index ({counter}).md")
                    else:
                        full_path = base_path.replace(".md", f" ({counter}).md")
                    counter += 1
                used_paths.add(full_path)

                yaml_lines = ["---"]
                for k, v in (note.properties or {}).items():
                    safe_v = str(v).replace('"', '\\"')
                    yaml_lines.append(f'{k}: "{safe_v}"')

                if note.due_at:
                    yaml_lines.append(f'due_at: "{note.due_at.strftime("%Y-%m-%dT%H:%M:%S")}"')
                yaml_lines.append(f'created_at: "{note.created_at.strftime("%Y-%m-%dT%H:%M:%S")}"')
                yaml_lines.append("---\n")

                content = "\n".join(yaml_lines) + block_utils.strip_markers(note.content or "")

                if note.attachments:
                    for att in note.attachments:
                        att_path = f"Assets/{att.filename}"
                        if att_path not in used_paths:
                            disk_path = os.path.join(app.config['UPLOAD_FOLDER'], att.filename)
                            if os.path.exists(disk_path):
                                zf.write(disk_path, att_path)
                                used_paths.add(att_path)

                        content = content.replace(f"/uploads/{att.filename}", f"Assets/{att.filename}")

                zf.writestr(full_path, content)

                for child in note.children:
                    process_note(child, next_parent_path)

            for child in root.children:
                process_note(child, "")

        memory_file.seek(0)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'notes_export_{timestamp}.zip'
        )

    @app.route('/api/searches/<int:search_id>/results')
    def api_search_results(search_id):
        s = SavedSearch.query.get_or_404(search_id)
        params = {}
        if s.params:
            from urllib.parse import parse_qsl
            params = dict(parse_qsl(s.params))

        q = params.get('q', '').strip()
        prop_key = params.get('prop_key', '').strip().lower()
        prop_val = params.get('prop_val', '').strip().lower()
        prop_has = params.get('prop_has', '').strip().lower()
        parent_filter = params.get('parent_filter', '').strip()
        exclude_status = params.get('exclude_status', '').strip().lower()
        due_date = params.get('due_date', '').strip()
        due_op = params.get('due_op', '').strip()

        if due_date == 'today':
            due_date = datetime.now().strftime('%Y-%m-%d')

        results = Note.query.filter(Note.deleted_at.is_(None)).all()

        if q:
            like_q = q.lower()
            results = [n for n in results
                       if like_q in (n.title or '').lower()
                       or like_q in (n.content or '').lower()]

        if prop_key:
            wanted_list = [w.strip().lower() for w in prop_val.split(',') if w.strip()]
            def matches(n):
                raw = (n.properties or {}).get(prop_key, '')
                if not wanted_list:
                    return bool(str(raw).strip())
                pieces = [p.strip().lower() for p in str(raw).split(',') if p.strip()]
                return all(w in pieces for w in wanted_list)
            results = [n for n in results if matches(n)]

        if prop_has:
            results = [n for n in results if prop_has in (n.properties or {})]

        if parent_filter and parent_filter.isdigit():
            results = [n for n in results if n.parent_id == int(parent_filter)]

        if exclude_status:
            exclude_list = [x.strip().lower() for x in exclude_status.split(',') if x.strip()]
            def not_excluded(n):
                raw = str((n.properties or {}).get('status', '')).strip().lower()
                pieces = [p.strip() for p in raw.split(',') if p.strip()]
                return not any(x in pieces for x in exclude_list)
            results = [n for n in results if not_excluded(n)]

        DUE_OPS = {'<', '<=', '=', '>=', '>'}
        if due_date and due_op in DUE_OPS:
            try:
                d = datetime.strptime(due_date, '%Y-%m-%d').date()
                def due_matches(n):
                    if n.due_at is not None:
                        target = n.due_at.date()
                    else:
                        raw = str((n.properties or {}).get('due') or '').strip()
                        if not re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
                            return False
                        try:
                            target = datetime.strptime(raw, '%Y-%m-%d').date()
                        except ValueError:
                            return False
                    if due_op == '<':  return target <  d
                    if due_op == '<=': return target <= d
                    if due_op == '=':  return target == d
                    if due_op == '>=': return target >= d
                    if due_op == '>':  return target >  d
                    return False
                results = [n for n in results if due_matches(n)]
            except ValueError:
                pass

        group_by = params.get('group_by', '').strip()
        sort_by = s.sort_by or 'modified'
        sort_dir = s.sort_dir or 'desc'
        reverse = (sort_dir == 'desc')

        if sort_by == 'title':
            results.sort(key=lambda n: (n.title or '').lower(), reverse=reverse)
        elif sort_by == 'created':
            results.sort(key=lambda n: n.created_at or datetime.min, reverse=reverse)
        elif sort_by == 'due':
            results.sort(key=lambda n: (n.due_at is None, n.due_at or datetime.max), reverse=reverse)
        else:
            results.sort(key=lambda n: n.updated_at or datetime.min, reverse=reverse)

        if group_by:
            def group_key(n):
                if group_by == '_due':
                    if n.due_at is not None:
                        return n.due_at.strftime('%Y-%m-%d')
                    raw = str((n.properties or {}).get('due') or '').strip()
                    return raw if raw and len(raw) == 10 else '~No due date'
                if group_by == '_parent':
                    return (n.parent.title if n.parent else 'Root')
                val = (n.properties or {}).get(group_by, '')
                return str(val) if val else '~(none)'

            results.sort(key=group_key, reverse=False)

        return jsonify([{'id': n.id, 'title': n.title} for n in results])

    @app.route('/sw.js')
    def service_worker():
        resp = send_from_directory(
            os.path.join(app.root_path, 'static'), 'sw.js',
            mimetype='application/javascript'
        )
        resp.headers['Service-Worker-Allowed'] = '/'
        resp.headers['Cache-Control'] = 'no-cache'
        return resp
        
    @app.route('/manifest.json')
    def manifest():
        return send_from_directory(
            os.path.join(app.root_path, 'static'), 'manifest.json',
            mimetype='application/manifest+json'
        )        

    def collect_prop_values():
        return get_collect_prop_values()

    def normalize_props(raw):
        out = {}
        for k, v in (raw or {}).items():
            nk = str(k).strip().lower()
            if not nk:
                continue
            if isinstance(v, str):
                if ',' in v:
                    pieces = []
                    seen = set()
                    for p in v.split(','):
                        t = p.strip().lower()
                        if t and t not in seen:
                            seen.add(t)
                            pieces.append(t)
                    out[nk] = ', '.join(pieces)
                else:
                    out[nk] = v.strip().lower()
            else:
                out[nk] = v
        return out
        
    def sort_props_status_last(props):
        """Forces the 'status' key to always be at the end of the properties dict."""
        if not props:
            return {}
        new_props = {k: v for k, v in props.items() if k != 'status'}
        if 'status' in props:
            new_props['status'] = props['status']
        return new_props        

    def ancestors_and_self(note):
        keep = set()
        n = note
        seen = set()
        while n and n.id not in seen:
            seen.add(n.id)
            keep.add(n.id)
            n = n.parent
        return keep

    def all_saved_searches():
        return SavedSearch.query.order_by(
            SavedSearch.pinned.desc(),
            SavedSearch.name.asc()
        ).all()

    def all_templates():
        return Note.query.filter_by(is_template=True).order_by(Note.title).all()

    @app.route('/')
    def index():
        import time; _t_start = time.time()
        print(f"[TIMING] after start: {time.time() - _t_start:.2f}s")
        root = get_root_note()
        root_id = root.id if root else None

        q = request.args.get('q', '').strip()
        prop_key = request.args.get('prop_key', '').strip().lower()
        prop_val = request.args.get('prop_val', '').strip().lower()
        prop_has = request.args.get('prop_has', '').strip().lower()
        parent_filter = request.args.get('parent_filter', '').strip()
        exclude_status = request.args.get('exclude_status', '').strip().lower()
        due_date_raw = request.args.get('due_date', '').strip()
        due_date = due_date_raw
        if due_date == 'today':
            due_date = datetime.now().strftime('%Y-%m-%d')

        due_op = request.args.get('due_op', '').strip()
        group_by = request.args.get('group_by', '').strip()
        current_search_id = request.args.get('search', type=int)
        parent_id = request.args.get('parent', type=int)
        raw_note = request.args.get('note', '')
        note_id = None
        if raw_note:
            if raw_note in ('pinned-section', 'recent-section', 'saved-search-section'):
                note_id = raw_note
            elif raw_note.isdigit():
                note_id = int(raw_note)
        focus = request.args.get('focus', '')
        url_sort_by = request.args.get('sort_by')
        url_sort_dir = request.args.get('sort_dir')
        clear_last = request.args.get('clear') == '1'
        force_empty = request.args.get('empty') == '1'

        view_mode = 'list'
        if current_search_id:
            saved_search = SavedSearch.query.get(current_search_id)
            if saved_search and saved_search.view_mode:
                view_mode = saved_search.view_mode

        has_due_filter = bool(due_date and due_op and due_op != 'any')
        has_any_date = (due_op == 'any')
        
        # If a saved search ID is specified, load its params and treat them as active
        if current_search_id:
            saved_search = SavedSearch.query.get(current_search_id)
            if saved_search and saved_search.params:
                from urllib.parse import parse_qsl
                # Merge saved search params with request params (request wins)
                saved_params = dict(parse_qsl(saved_search.params))
                if not q: q = saved_params.get('q', '')
                if not prop_key: prop_key = saved_params.get('prop_key', '').strip().lower()
                if not prop_val: prop_val = saved_params.get('prop_val', '').strip().lower()
                if not prop_has: prop_has = saved_params.get('prop_has', '').strip().lower()
                if not parent_filter: parent_filter = saved_params.get('parent_filter', '').strip()
                if not exclude_status: exclude_status = saved_params.get('exclude_status', '').strip().lower()
                if not due_date: due_date = saved_params.get('due_date', '').strip()
                if not due_op: due_op = saved_params.get('due_op', '').strip()
                if not group_by: group_by = saved_params.get('group_by', '').strip()
                # Recompute due_date 'today'
                if due_date == 'today':
                    due_date = datetime.now().strftime('%Y-%m-%d')
        
        has_due_filter = bool(due_date and due_op and due_op != 'any')
        has_any_date = (due_op == 'any')
        search_active = bool(
            q or prop_key or exclude_status
            or has_due_filter or has_any_date
            or prop_has or parent_filter
            or current_search_id
        )

        if note_id is not None:
            search_active = False

        if (note_id is None and parent_id is None
                and not search_active and not clear_last
                and not force_empty):
            last = request.cookies.get('last_note_id', type=int)
            if last:
                note_id = last

        if note_id is None and not search_active and root_id and not force_empty:
            note_id = root_id

        from sqlalchemy.orm import selectinload
        if note_id:
            if str(note_id) == 'pinned-section':
                selected_note = VirtualNote('pinned-section', 'Pinned', True)
            elif str(note_id) == 'recent-section':
                selected_note = VirtualNote('recent-section', 'Recent', True)
            elif str(note_id) == 'saved-search-section':
                selected_note = VirtualNote('saved-search-section', 'Searches', True)
            else:
                selected_note = Note.query.options(
                    selectinload(Note.children).selectinload(Note.children)
                ).get(int(note_id))
            
            if selected_note is None or getattr(selected_note, 'deleted_at', None) is not None:
                if root_id:
                    return redirect(url_for('index', note=root_id))
                else:
                    return redirect(url_for('index'))
        else:
            selected_note = None
            
        # ── Track recently viewed notes ──
        if selected_note and not isinstance(selected_note, VirtualNote) and getattr(selected_note, 'id', None):
            try:
                recent_str = Setting.get('recent_viewed_ids', '')
                recent_ids = [int(x) for x in recent_str.split(',') if x.isdigit()]
                
                # Move this note to the front
                if selected_note.id in recent_ids:
                    recent_ids.remove(selected_note.id)
                recent_ids.insert(0, selected_note.id)
                
                # Keep only the top 10 (give a little buffer, we'll slice to 3 in the view)
                recent_ids = recent_ids[:10]
                Setting.set('recent_viewed_ids', ','.join(str(i) for i in recent_ids))
            except Exception as e:
                print(f"[recent_notes tracking] failed: {e}")            

        # Properties passed as a dict; the template will |tojson it so
        # `</script>` in a value can no longer break out of the inline
        # <script> block.
        note_properties_dict = (selected_note.properties or {}) if selected_note else {}

        note_updated_at = (
            selected_note.updated_at.isoformat() + 'Z'
            if selected_note and selected_note.updated_at else ''
        )

        sort_source = None
        if not search_active:
            if selected_note and selected_note.id != root_id and selected_note.children:
                sort_source = selected_note
            elif parent_id:
                sort_source = Note.query.get(parent_id)

        all_prop_values = collect_prop_values()
        # Fetch the distinct keys directly from the DB for the dropdowns
        # This bypasses any stale cache issues.
        from sqlalchemy import text
        try:
            raw_keys = db.session.execute(text("SELECT DISTINCT key FROM (SELECT DISTINCT j.key FROM note, json_each(note.properties) AS j)")).all()
            # Merge the fresh keys into all_prop_values so the template sees them
            for row in raw_keys:
                if row[0] and str(row[0]).strip():
                    k = str(row[0]).strip().lower()
                    if k not in all_prop_values:
                        all_prop_values[k] = [] # empty list, just so the key exists
        except Exception as e:
            print("Failed to get fresh keys:", e)        
        print(f"[TIMING] after prop_values: {time.time() - _t_start:.2f}s")

        if search_active:
            is_valid_sort = url_sort_by in SORT_OPTIONS or url_sort_by in all_prop_values
            sort_by = url_sort_by if is_valid_sort else 'modified'
            sort_dir = url_sort_dir if url_sort_dir in DIR_OPTIONS else 'desc'
        elif sort_source:
            sort_by = sort_source.sort_by or 'modified'
            sort_dir = sort_source.sort_dir or 'asc'
            if sort_by not in SORT_OPTIONS: sort_by = 'modified'
            if sort_dir not in DIR_OPTIONS: sort_dir = 'asc'
        else:
            sort_by = Setting.get('root_sort_by', 'modified')
            sort_dir = Setting.get('root_sort_dir', 'asc')
            if sort_by not in SORT_OPTIONS: sort_by = 'modified'
            if sort_dir not in DIR_OPTIONS: sort_dir = 'asc'

        results = []
        if search_active:
            query = Note.query.filter(Note.deleted_at.is_(None)) # <--- ADDED
            if q:
                clean_q = strip_accents(q)
                query = query.filter(db.or_(
                    db.func.STRIP_ACCENTS(Note.title).contains(clean_q),
                    db.func.STRIP_ACCENTS(Note.content).contains(clean_q)
                ))
                                            
            results = query.options(db.joinedload(Note.children)).all()
            if prop_key:
                wanted_list = [w.strip().lower() for w in prop_val.split(',') if w.strip()]
                def matches(n):
                    raw = (n.properties or {}).get(prop_key, '')
                    if not wanted_list:
                        return bool(str(raw).strip())
                    pieces = [p.strip().lower() for p in str(raw).split(',') if p.strip()]
                    return all(w in pieces for w in wanted_list)
                results = [n for n in results if matches(n)]
            if prop_has:
                results = [n for n in results if prop_has in (n.properties or {})]
            if exclude_status:
                exclude_list = [x.strip().lower() for x in exclude_status.split(',') if x.strip()]
                def not_excluded(n):
                    raw = str((n.properties or {}).get('status', '')).strip().lower()
                    pieces = [p.strip() for p in raw.split(',') if p.strip()]
                    return not any(x in pieces for x in exclude_list)
                results = [n for n in results if not_excluded(n)]
            if has_due_filter and due_op in DUE_OPS:
                try:
                    d = datetime.strptime(due_date, '%Y-%m-%d').date()
                    def due_matches(n):
                        if n.due_at is not None:
                            target = n.due_at.date()
                        else:
                            raw = str((n.properties or {}).get('due') or '').strip()
                            if not re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
                                return False
                            try:
                                target = datetime.strptime(raw, '%Y-%m-%d').date()
                            except ValueError:
                                return False
                        if due_op == '<':  return target <  d
                        if due_op == '<=': return target <= d
                        if due_op == '=':  return target == d
                        if due_op == '>=': return target >= d
                        if due_op == '>':  return target >  d
                        return False
                    results = [n for n in results if due_matches(n)]
                except ValueError:
                    pass

            if parent_filter and parent_filter.isdigit():
                results = [n for n in results if n.parent_id == int(parent_filter)]
            sort_notes(results, sort_by, sort_dir)

        if root:
            root_sort = Setting.get('root_sort_by', 'modified')
            root_sort_dir = Setting.get('root_sort_dir', 'desc')
            if root_sort not in SORT_OPTIONS: root_sort = 'modified'
            if root_sort_dir not in DIR_OPTIONS: root_sort_dir = 'desc'

            sorted_tops, total_top_level = get_top_level_children(
                root,
                sort_by=root_sort,
                sort_dir=root_sort_dir,
            )
        else:
            sorted_tops = []
            total_top_level = 0

        if search_active:
            tops_sort_by, tops_sort_dir = sort_by, sort_dir
        else:
            tops_sort_by = Setting.get('root_sort_by', 'modified')
            tops_sort_dir = Setting.get('root_sort_dir', 'asc')
            if tops_sort_by not in SORT_OPTIONS: tops_sort_by = 'modified'
            if tops_sort_dir not in DIR_OPTIONS: tops_sort_dir = 'asc'

        current = selected_note

        suffix_params = {k: v for k, v in {
            'q': q, 'prop_key': prop_key, 'prop_val': prop_val,
            'exclude_status': exclude_status,
            'due_date': due_date_raw,
            'due_op': due_op,
            'prop_has': prop_has,
            'parent_filter': parent_filter,
        }.items() if v}
        query_suffix = urlencode(suffix_params) if suffix_params else ''

        status_values = _split_prop_values(all_prop_values.get('status', []))

        pinned_ids_str = Setting.get('pinned_note_ids', '')
        pinned_ids = [int(x) for x in pinned_ids_str.split(',') if x.isdigit()]
        pinned_notes = Note.query.filter(Note.id.in_(pinned_ids)).order_by(Note.title.asc()).all() if pinned_ids else []
        
        # --- INJECT CUSTOM VIRTUAL FOLDERS ---
        virtual_notes = []
        if pinned_notes:
            virtual_notes.append(VirtualNote('pinned-section', 'Pinned', True))
        virtual_notes.append(VirtualNote('recent-section', 'Recent', True))
        if all_saved_searches():
            virtual_notes.append(VirtualNote('saved-search-section', 'Searches', True))

        # Combine them with the real notes (virtual notes first)
        sorted_tops = virtual_notes + sorted_tops
        total_top_level += len(virtual_notes)
        # --------------------------------------        

        print(f"[VIRTUAL] sorted_tops count = {len(sorted_tops)}")
        print(f"[VIRTUAL] virtual_notes = {[v.title for v in virtual_notes]}")
        print(f"[VIRTUAL] sorted_tops titles = {[n.title for n in sorted_tops[:5]]}")        
        
        # Build the sidebar context
        sidebar_ctx = dict(
            sorted_tops=sorted_tops,
            current=current,
            current_search_id=current_search_id,
            query_suffix=query_suffix,
            root_id=root_id,
            root_sort_by=tops_sort_by,
            root_sort_dir=tops_sort_dir,
            saved_searches=all_saved_searches(),
            pinned_notes=pinned_notes,
            pinned_ids=pinned_ids,
            total_top_level=total_top_level,
            search_q=q,
            prop_key=prop_key,
            due_date=due_date,
            due_op=due_op,
            exclude_status=exclude_status,
            parent_notes=get_parent_notes(),
            all_prop_values=all_prop_values,
            status_values=status_values,
        )
        
        # Render the sidebar (this will pull from cache if available)
        sidebar_html = get_cached_sidebar_html(app, sidebar_ctx)        

        # Fetch the name of the saved search if one is active
        current_search_name = ''
        if current_search_id:
            saved_search = SavedSearch.query.get(current_search_id)
            if saved_search:
                current_search_name = saved_search.name

        resp = make_response(render_template(
            'index.html',
            sidebar_html=sidebar_html,            
            view_mode=view_mode,
            note_properties_dict=note_properties_dict,
            note_updated_at=note_updated_at,
            sorted_tops=sorted_tops,
            visible_ids=None,
            search_active=search_active,
            current=current,
            current_parent=parent_id,
            current_sort=sort_by,
            current_sort_dir=sort_dir,
            search_q=q,
            prop_key=prop_key,
            prop_val=prop_val,
            exclude_status=exclude_status,
            due_date=due_date,
            due_date_raw=due_date_raw,
            due_op=due_op,
            query_suffix=query_suffix,
            focus=focus,
            saved_searches=all_saved_searches(),
            templates=all_templates(),
            all_prop_values=all_prop_values,
            status_values=status_values,
            root_id=root_id,
            root_sort_by=tops_sort_by,
            root_sort_dir=tops_sort_dir,
            results=results,
            group_by=group_by,
            search_query=q,
            now_date=datetime.now().strftime('%Y-%m-%d'),
            outline_view_mode=Setting.get('outline_view_mode', 'card'),
            pinned_notes=pinned_notes,
            pinned_ids=pinned_ids,
            parent_notes=get_parent_notes(),
            current_note_title=(selected_note.title or '').lower() if selected_note else '',
            total_top_level=total_top_level,
            current_search_id=current_search_id,     
            current_search_name=current_search_name,            
            tab_bar_position=Setting.get('tab_bar_position', 'top'),
        ))
        if current and not search_active and not clear_last:
            resp.set_cookie('last_note_id', str(current.id),
                            max_age=60*60*24*365, samesite='Lax')
        print(f"[TIMING] index() took {time.time() - _t_start:.2f}s")
        return resp
        
    @app.route('/api/dashboard/save_todo_order', methods=['POST'])
    def save_todo_order():
        data = request.get_json() or {}
        order = data.get('order', [])
        
        # Save ONE global order for all tasks
        # If the order array is empty, this clears the manual sort
        Setting.set('dashboard_todo_order', ','.join(str(x) for x in order))
            
        # SMART UPDATE
        updates = data.get('priority_updates', {})
        if updates:
            for note_id_str, new_prio in updates.items():
                note = Note.query.get(int(note_id_str))
                if note:
                    props = dict(note.properties or {})
                    if str(props.get('priority')) != str(new_prio):
                        props['priority'] = str(new_prio)
                        note.properties = sort_props_status_last(props)
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(note, "properties")
                        
        db.session.commit()
        return jsonify({'ok': True})     
        
    @app.route('/dashboard')
    def dashboard():
        root = get_root_note()
        
        # 1. Pinned Notes
        pinned_ids_str = Setting.get('pinned_note_ids', '')
        pinned_ids = [int(x) for x in pinned_ids_str.split(',') if x.isdigit()]
        pinned_notes = Note.query.filter(Note.id.in_(pinned_ids), Note.deleted_at.is_(None)).order_by(Note.title.asc()).all() if pinned_ids else []
        # Fetch the most recently viewed notes
        recent_str = Setting.get('recent_viewed_ids', '')
        recent_viewed_ids = [int(x) for x in recent_str.split(',') if x.isdigit()]
        
        # Take the top 3
        recent_viewed_ids = recent_viewed_ids[:3]
        
        recent_notes = []
        if recent_viewed_ids:
            # Fetch them and re-sort them to match the order of recent_viewed_ids
            notes_dict = {n.id: n for n in Note.query.filter(Note.id.in_(recent_viewed_ids), Note.deleted_at.is_(None)).all()}
            recent_notes = [notes_dict[i] for i in recent_viewed_ids if i in notes_dict]
            
        # Remove duplicates from recent_notes (if it's already in pinned, skip it)
        recent_notes = [n for n in recent_notes if n.id not in pinned_ids]            

        # 2. Saved Searches
        saved_searches = SavedSearch.query.order_by(SavedSearch.name.asc()).all()
        
        # 3. Find Default Search ("Due")
        default_search_id = None
        for s in saved_searches:
            if s.name.lower() == 'due':
                default_search_id = s.id
                break
        
        current_search_id = request.args.get('search_id', type=int) or default_search_id
        search_results = []
        current_search_name = ''

        # 4. Execute Search Logic
        if current_search_id:
            s = SavedSearch.query.get(current_search_id)
            if s:
                current_search_name = s.name
                params = {}
                if s.params:
                    from urllib.parse import parse_qsl
                    params = dict(parse_qsl(s.params))
                
                q = params.get('q', '').strip()
                prop_key = params.get('prop_key', '').strip().lower()
                prop_val = params.get('prop_val', '').strip().lower()
                prop_has = params.get('prop_has', '').strip().lower()
                parent_filter = params.get('parent_filter', '').strip()
                exclude_status = params.get('exclude_status', '').strip().lower()
                due_date = params.get('due_date', '').strip()
                due_op = params.get('due_op', '').strip()

                results = Note.query.filter(Note.deleted_at.is_(None)).all()

                if q:
                    like_q = q.lower()
                    results = [n for n in results if like_q in (n.title or '').lower() or like_q in (n.content or '').lower()]

                if prop_key:
                    wanted_list = [w.strip().lower() for w in prop_val.split(',') if w.strip()]
                    def matches(n):
                        raw = (n.properties or {}).get(prop_key, '')
                        if not wanted_list:
                            return bool(str(raw).strip())
                        pieces = [p.strip().lower() for p in str(raw).split(',') if p.strip()]
                        return all(w in pieces for w in wanted_list)
                    results = [n for n in results if matches(n)]

                if prop_has:
                    results = [n for n in results if prop_has in (n.properties or {})]

                if parent_filter and parent_filter.isdigit():
                    results = [n for n in results if n.parent_id == int(parent_filter)]

                if exclude_status:
                    exclude_list = [x.strip().lower() for x in exclude_status.split(',') if x.strip()]
                    def not_excluded(n):
                        raw = str((n.properties or {}).get('status', '')).strip().lower()
                        pieces = [p.strip() for p in raw.split(',') if p.strip()]
                        return not any(x in pieces for x in exclude_list)
                    results = [n for n in results if not_excluded(n)]

                DUE_OPS = {'<', '<=', '=', '>=', '>'}
                if due_date == 'today':
                    due_date = datetime.now().strftime('%Y-%m-%d')
                    
                if due_date and due_op in DUE_OPS:
                    try:
                        d = datetime.strptime(due_date, '%Y-%m-%d').date()
                        def due_matches(n):
                            if n.due_at is not None:
                                target = n.due_at.date()
                            else:
                                raw = str((n.properties or {}).get('due') or '').strip()
                                if not re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
                                    return False
                                try:
                                    target = datetime.strptime(raw, '%Y-%m-%d').date()
                                except ValueError:
                                    return False
                            if due_op == '<':  return target <  d
                            if due_op == '<=': return target <= d
                            if due_op == '=':  return target == d
                            if due_op == '>=': return target >= d
                            if due_op == '>':  return target >  d
                            return False
                        results = [n for n in results if due_matches(n)]
                    except ValueError:
                        pass

                # Filter out Canceled notes
                def not_canceled(n):
                    props = n.properties or {}
                    status = str(props.get('status', '')).lower()
                    tags = str(props.get('tags', '')).lower()
                    if 'canceled' in status or 'cancelled' in status: return False
                    if 'canceled' in tags or 'cancelled' in tags: return False
                    return True
                
                results = [n for n in results if not_canceled(n)]

                # --- SORTING LOGIC ---
                today_date = datetime.now().date()
                
                # Special sorting for date-based searches (Due, Upcoming, etc.)
                if due_op:
                    def due_sort(n):
                        props = n.properties or {}
                        
                        due_str = str(props.get('due', '')).strip()
                        if not due_str and n.due_at:
                            due_str = n.due_at.strftime('%Y-%m-%d')
                        
                        try:
                            due_val = datetime.strptime(due_str, '%Y-%m-%d').date()
                            if due_val < today_date:
                                n._is_overdue = True
                            else:
                                n._is_overdue = False
                        except (ValueError, TypeError):
                            due_val = datetime.max.date()
                            n._is_overdue = False
                        
                        raw_p = props.get('priority')
                        p_str = str(raw_p) if raw_p is not None else ''
                        p_str = p_str.strip()
                        if p_str.endswith('.0'): p_str = p_str[:-2]
                        try: p_val = float(p_str)
                        except ValueError: p_val = float('inf')
                            
                        u_str = str(props.get('urgency', '')).strip()
                        try: u_val = float(u_str)
                        except ValueError: u_val = float('inf')
                            
                        return (due_val, p_val, u_val)

                    results.sort(key=due_sort)
                else:
                    # Normal sorting (using the search's own sort_by and sort_dir)
                    sort_by = s.sort_by or 'modified'
                    sort_dir = s.sort_dir or 'desc'
                    reverse = (sort_dir == 'desc')

                    if sort_by == 'title':
                        results.sort(key=lambda n: (n.title or '').lower(), reverse=reverse)
                    elif sort_by == 'created':
                        results.sort(key=lambda n: n.created_at or datetime.min, reverse=reverse)
                    elif sort_by == 'due':
                        results.sort(key=lambda n: (n.due_at is None, n.due_at or datetime.max), reverse=reverse)
                    elif sort_by == 'modified':
                        results.sort(key=lambda n: n.updated_at or datetime.min, reverse=reverse)
                    else:
                        # Sort by property key
                        def prop_sort(n):
                            val = (n.properties or {}).get(sort_by, '')
                            val_str = str(val).strip().lower()
                            if not val_str:
                                return (1, 0, '') if not reverse else (-1, 0, '')
                            try:
                                num = float(val_str)
                                return (0, num, '')
                            except ValueError:
                                return (0, 0, val_str)
                        results.sort(key=prop_sort, reverse=reverse)

                search_results = results[:50]

        # 5. Priority/Todo Tasks logic — see get_dashboard_scan()
        scan = get_dashboard_scan()
        available_priorities = list(scan['available_priorities'])
        available_urgencies = list(scan['available_urgencies'])
        all_todo_candidates = list(scan['todo_tasks'])

        def _prio_key(p):
            try:
                return (0, float(p))
            except ValueError:
                return (1, p)

        selected_priority = request.args.get('priority', '1').strip()
        if 'all' not in available_priorities:
            available_priorities.insert(0, 'all')
        if selected_priority and selected_priority not in available_priorities:
            available_priorities.insert(0, selected_priority)
            available_priorities = sorted(set(available_priorities), key=_prio_key)

        selected_urgency = request.args.get('urgency', '1').strip()
        if 'all' not in available_urgencies:
            available_urgencies.insert(0, 'all')
        if selected_urgency and selected_urgency not in available_urgencies:
            available_urgencies.insert(0, selected_urgency)
            available_urgencies = sorted(set(available_urgencies), key=_prio_key)

        # Apply the priority/urgency filters (the scan is cached across
        # requests, so filtering here is cheap and keeps the cache generic).
        todo_tasks = []
        for n in all_todo_candidates:
            if selected_priority != 'all' and n._priority_val != selected_priority:
                continue
            if selected_urgency and selected_urgency != 'all' and n._urgency != selected_urgency:
                continue
            todo_tasks.append(n)

        # Sort Todo
        # Read the saved sort mode from the database
        saved_sort_mode = Setting.get('dashboard_todo_sort_mode', 'auto')
        
        # Check the URL first (so the toggle button still works), then fall back to DB
        todo_sort_mode = request.args.get('todo_sort', saved_sort_mode)
        
        # Pass to template for highlighting
        is_manual_sort = (todo_sort_mode == 'manual')
        
        def auto_sort_key(note):
            try: p_val = float(note._priority_val)
            except ValueError: p_val = float('inf')
            
            if not note._urgency: u_val = float('inf')
            else:
                try: u_val = float(note._urgency)
                except ValueError: u_val = float('inf')
                
            return (p_val, u_val)
            
        todo_tasks.sort(key=auto_sort_key)

        if todo_sort_mode == 'manual':
            saved_order_str = Setting.get('dashboard_todo_order', '')
            saved_ids = [int(x) for x in saved_order_str.split(',') if x.isdigit()]
            
            if saved_ids:
                task_map = {n.id: n for n in todo_tasks}
                sorted_tasks = []
                
                for tid in saved_ids:
                    if tid in task_map:
                        sorted_tasks.append(task_map[tid])
                        del task_map[tid]
                
                new_tasks = list(task_map.values())
                todo_tasks = sorted_tasks + new_tasks
            
        todo_tasks = todo_tasks[:50]

        # 6. Embedded note (custom pick, or today's journal as fallback)
        journal = _get_journal_parent()
        today_title = substitute_vars('{{date}}')
        journal_note = Note.query.filter_by(
            parent_id=journal.id, title=today_title, deleted_at=None
        ).first()

        if not journal_note:
            journal_note = Note(title=today_title, content='', properties={}, id=None)

        embedded_note = None
        stored_embed_id = Setting.get('dashboard_embed_note_id', '').strip()
        if stored_embed_id.isdigit():
            candidate = Note.query.get(int(stored_embed_id))
            if candidate and candidate.deleted_at is None:
                embedded_note = candidate
            else:
                # Stale setting — clear it so we fall back on next render.
                Setting.set('dashboard_embed_note_id', '')

        if embedded_note is None:
            embedded_note = journal_note

        # Sidebar context
        root_sort_by = Setting.get('root_sort_by', 'modified')
        root_sort_dir = Setting.get('root_sort_dir', 'asc')
        sorted_tops, total_top_level = get_top_level_children(root, root_sort_by, root_sort_dir)

        # Inject Virtual Notes into Dashboard sidebar too
        virtual_notes = []
        if pinned_notes:
            virtual_notes.append(VirtualNote('pinned-section', 'Pinned', True))
        virtual_notes.append(VirtualNote('recent-section', 'Recent', True))
        if all_saved_searches():
            virtual_notes.append(VirtualNote('saved-search-section', 'Searches', True))

        sorted_tops = virtual_notes + sorted_tops
        total_top_level += len(virtual_notes)

        return render_template(
            'dashboard.html',
            pinned_notes=pinned_notes,
            saved_searches=saved_searches,
            dashboard_search_id=current_search_id,
            search_active_sidebar=False,
            current_search_name=current_search_name,
            search_results=search_results,
            todo_tasks=todo_tasks,
            selected_priority=selected_priority,
            available_priorities=available_priorities,
            selected_urgency=selected_urgency,
            available_urgencies=available_urgencies,
            today_note=embedded_note,
            sorted_tops=sorted_tops,
            total_top_level=total_top_level,
            root_id=root.id if root else None,
            root_sort_by=root_sort_by,
            root_sort_dir=root_sort_dir,
            all_prop_values=collect_prop_values(),
            parent_notes=get_parent_notes(),
            current=None,
            query_suffix='',
            search_q='', prop_key='', prop_val='', due_date='', due_op='', exclude_status='',
            templates=all_templates(),
            pinned_ids=pinned_ids,
            status_values=[],
            outline_view_mode=Setting.get('outline_view_mode', 'card'),
            now_date=datetime.now().strftime('%Y-%m-%d'),
            is_manual_sort=is_manual_sort,
            embedded_note_id=(embedded_note.id if embedded_note else None),
            embedded_is_today=(
                embedded_note is not None
                and journal_note is not None
                and embedded_note.id == journal_note.id
            ),
            recent_notes=recent_notes,            
        )
        
    @app.route('/api/dashboard/save_sort_mode', methods=['POST'])
    def save_sort_mode():
        data = request.get_json() or {}
        mode = data.get('mode', 'auto')
        if mode not in ('auto', 'manual'):
            mode = 'auto'
        Setting.set('dashboard_todo_sort_mode', mode)
        return jsonify({'ok': True})

    @app.route('/api/dashboard/embed', methods=['POST'])
    def dashboard_embed():
        data = request.get_json() or {}
        note_id = data.get('note_id')

        if note_id in (None, '', 0):
            Setting.set('dashboard_embed_note_id', '')
            return jsonify({'ok': True, 'id': None})

        if not str(note_id).isdigit():
            return jsonify({'ok': False, 'error': 'invalid id'}), 400

        n = Note.query.get(int(note_id))
        if not n or n.deleted_at is not None:
            return jsonify({'ok': False, 'error': 'note not found'}), 404

        Setting.set('dashboard_embed_note_id', str(n.id))
        return jsonify({'ok': True, 'id': n.id, 'title': n.title})
        
    @app.route('/api/dashboard/save_section_state', methods=['POST'])
    def dashboard_save_section_state():
        data = request.get_json() or {}
        section = str(data.get('section', '')).strip()
        is_open = bool(data.get('open', True))

        if section and section.startswith('dash-sec-'):
            Setting.set(section, 'open' if is_open else 'closed')
            return jsonify({'ok': True})

        return jsonify({'ok': False, 'error': 'invalid section'}), 400        
        
    @app.route('/settings', methods=['GET', 'POST'])
    def settings():
        if request.method == 'POST':
            default_tpl = request.form.get('default_template_id', '').strip()
            Setting.set('default_template_id', default_tpl)

            task_tpl = request.form.get('task_template_id', '').strip()
            Setting.set('task_template_id', task_tpl)

            daily_note_tpl = request.form.get('daily_note_template_id', '').strip()
            Setting.set('daily_note_template_id', daily_note_tpl)

            resize_pct = request.form.get('image_resize_percent', '100').strip()
            Setting.set('image_resize_percent', resize_pct)

            journal_id = request.form.get('journal_parent_id', '').strip()
            Setting.set('journal_parent_id', journal_id)

            quick_note_id = request.form.get('quick_note_parent_id', '').strip()
            Setting.set('quick_note_parent_id', quick_note_id)

            map_note_parent_id = request.form.get('map_note_parent_id', '').strip()
            Setting.set('map_note_parent_id', map_note_parent_id)

            ntfy = request.form.get('ntfy_url', '').strip()
            Setting.set('ntfy_url', ntfy)

            Setting.set('outline_view_mode', request.form.get('outline_view_mode', 'card'))
            Setting.set('tab_bar_position', request.form.get('tab_bar_position', 'top'))

            # Map defaults
            Setting.set('map_default_lat',  request.form.get('map_default_lat',  '-23.2208').strip() or '-23.2208')
            Setting.set('map_default_lng',  request.form.get('map_default_lng',  '-44.7136').strip() or '-44.7136')
            Setting.set('map_default_zoom', request.form.get('map_default_zoom', '13').strip() or '13')

            # Dashboard Sections
            for sec in ['pinned', 'search', 'todo', 'journal']:
                show_vals = request.form.getlist(f'dash_show_{sec}')
                show = '1' if '1' in show_vals else '0'
                order = request.form.get(f'dash_order_{sec}', '1')
                Setting.set(f'dashboard_show_{sec}', show)
                Setting.set(f'dashboard_order_{sec}', order)

            return redirect(url_for('settings'))

        default_tpl = Setting.get('default_template_id', '')
        task_tpl_id = Setting.get('task_template_id', '')
        daily_note_tpl_id = Setting.get('daily_note_template_id', '')
        image_resize_percent = Setting.get('image_resize_percent', '100')
        journal_parent_id = Setting.get('journal_parent_id', '')
        quick_note_parent_id = Setting.get('quick_note_parent_id', '')
        map_note_parent_id = Setting.get('map_note_parent_id', '')
        ntfy_url = Setting.get('ntfy_url', app.config['NTFY_URL'])
        map_default_lat  = Setting.get('map_default_lat',  '-23.2208')
        map_default_lng  = Setting.get('map_default_lng',  '-44.7136')
        map_default_zoom = Setting.get('map_default_zoom', '13')
        root = get_root_note()
        
        root_sort_by = Setting.get('root_sort_by', 'modified')
        root_sort_dir = Setting.get('root_sort_dir', 'asc')
        if root_sort_by not in SORT_OPTIONS: root_sort_by = 'modified'
        if root_sort_dir not in DIR_OPTIONS: root_sort_dir = 'asc'

        sorted_tops, total_top_level = get_top_level_children(root, root_sort_by, root_sort_dir)
        
        pinned_ids_str = Setting.get('pinned_note_ids', '')
        pinned_ids = [int(x) for x in pinned_ids_str.split(',') if x.isdigit()]
        pinned_notes = Note.query.filter(Note.id.in_(pinned_ids)).order_by(Note.title.asc()).all() if pinned_ids else []
        
        dash_settings = {
            'pinned': (Setting.get('dashboard_show_pinned', '1') == '1', Setting.get('dashboard_order_pinned', '1')),
            'search': (Setting.get('dashboard_show_search', '1') == '1', Setting.get('dashboard_order_search', '2')),
            'todo':   (Setting.get('dashboard_show_todo', '1') == '1',   Setting.get('dashboard_order_todo', '3')),
            'journal':(Setting.get('dashboard_show_journal', '1') == '1',Setting.get('dashboard_order_journal', '4')),
        }        

        return render_template(
            'settings.html',
            templates=all_templates(),
            default_template_id=default_tpl,
            task_template_id=task_tpl_id,
            daily_note_template_id=daily_note_tpl_id,
            image_resize_percent=image_resize_percent,
            journal_parent_id=journal_parent_id,
            quick_note_parent_id=quick_note_parent_id,
            map_note_parent_id=map_note_parent_id,
            ntfy_url=ntfy_url,
            map_default_lat=map_default_lat,
            map_default_lng=map_default_lng,
            map_default_zoom=map_default_zoom,
            all_prop_values=collect_prop_values(),
            all_notes=Note.query.all(),
            sorted_tops=sorted_tops,
            pinned_notes=pinned_notes,
            pinned_ids=pinned_ids,
            total_top_level=total_top_level,
            saved_searches=all_saved_searches(),
            notes=[],
            current=None,
            current_parent=None,
            current_sort='modified',
            current_sort_dir='asc',
            search_q='', prop_key='', prop_val='',
            due_date='', due_op='',
            query_suffix='', search_active=False,
            root_id=root.id if root else None,
            root_sort_by=root_sort_by,
            root_sort_dir=root_sort_dir,
            results=[],
            group_by='',
            outline_view_mode=Setting.get('outline_view_mode', 'card'),
            tab_bar_position=Setting.get('tab_bar_position', 'top'),
            parent_notes=get_parent_notes(),
            status_values=[],
            exclude_status='',
            dash_settings=dash_settings,            
        )

    @app.route('/tags')
    def tags():
        TAGS_KEY = 'tags'

        tag_index = {}

        for n in Note.query.all():
            props = n.properties or {}
            raw = None
            for k, v in props.items():
                if str(k).strip().lower() == TAGS_KEY:
                    raw = v
                    break
            if raw is None:
                continue

            seen_in_this_note = set()
            for piece in str(raw).split(','):
                val = piece.strip().lower()
                if not val or val in seen_in_this_note:
                    continue
                seen_in_this_note.add(val)

                entry = tag_index.setdefault(val, {'notes': []})
                entry['notes'].append(n)

        grouped = {}
        for val in sorted(tag_index.keys()):
            notes = tag_index[val]['notes']
            notes.sort(key=lambda x: (x.title or '').lower())
            grouped[val] = {
                'note_count': len(notes),
                'notes': notes,
            }

        root = get_root_note()
        root_sort_by = Setting.get('root_sort_by', 'modified')
        root_sort_dir = Setting.get('root_sort_dir', 'asc')

        sorted_tops = _sorted_children(
            root.children if root else [],
            root_sort_by,
            root_sort_dir
        )

        # Fetch top-level notes with has_children set (like index does)
        sorted_tops, total_top_level = get_top_level_children(root, root_sort_by, root_sort_dir)
        
        # Get pinned notes
        pinned_ids_str = Setting.get('pinned_note_ids', '')
        pinned_ids = [int(x) for x in pinned_ids_str.split(',') if x.isdigit()]
        pinned_notes = Note.query.filter(Note.id.in_(pinned_ids)).order_by(Note.title.asc()).all() if pinned_ids else []

        return render_template(
            'tags.html',
            grouped=grouped,
            total_notes=sum(entry['note_count'] for entry in grouped.values()),
            sorted_tops=sorted_tops,          # <--- Ensure this uses the new list
            pinned_notes=pinned_notes,        # <--- Add this
            pinned_ids=pinned_ids,            # <--- Add this
            total_top_level=total_top_level,  # <--- Add this
            root_id=root.id if root else None,
            root_sort_by=root_sort_by,
            root_sort_dir=root_sort_dir,
            all_prop_values=collect_prop_values(),
            templates=all_templates(),
            saved_searches=all_saved_searches(),
            all_notes=Note.query.all(),
            notes=[],
            current=None,
            current_parent=None,
            current_sort='modified',
            current_sort_dir='asc',
            search_q='', prop_key='', prop_val='',
            due_date='', due_op='',
            query_suffix='', search_active=False,
            results=[], group_by='',
            parent_notes=get_parent_notes(),     # <--- Add this
            status_values=[],                    # <--- Add this
            exclude_status='',
        )

    @app.route('/properties')
    def properties():
        EXCLUDED_KEYS = {'tags'}

        index = {}
        for n in Note.query.all():
            for k, v in (n.properties or {}).items():
                if not k:
                    continue
                key = str(k).strip().lower()
                if key in EXCLUDED_KEYS:
                    continue

                entry = index.setdefault(key, {'values': {}, 'notes': set()})

                val_found = False
                for piece in str(v).split(','):
                    val = piece.strip().lower()
                    if not val:
                        continue
                    val_found = True
                    entry['values'][val] = entry['values'].get(val, 0) + 1

                if val_found:
                    entry['notes'].add(n.id)

        grouped = {}
        for k in sorted(index.keys()):
            entry = index[k]
            if not entry['values']:
                continue
            grouped[k] = {
                'note_count': len(entry['notes']),
                'values': sorted(entry['values'].items(), key=lambda kv: kv[0]),
                'value_count': len(entry['values']),
            }

        root = get_root_note()
        root_sort_by = Setting.get('root_sort_by', 'modified')
        root_sort_dir = Setting.get('root_sort_dir', 'asc')

        sorted_tops = _sorted_children(
            root.children if root else [],
            root_sort_by,
            root_sort_dir
        )
        
        # Fetch top-level notes with has_children set
        sorted_tops, total_top_level = get_top_level_children(root, root_sort_by, root_sort_dir)
        
        # Get pinned notes
        pinned_ids_str = Setting.get('pinned_note_ids', '')
        pinned_ids = [int(x) for x in pinned_ids_str.split(',') if x.isdigit()]
        pinned_notes = Note.query.filter(Note.id.in_(pinned_ids)).order_by(Note.title.asc()).all() if pinned_ids else []

        return render_template(
            'properties.html',
            grouped=grouped,
            total_notes=sum(entry['note_count'] for entry in grouped.values()),
            sorted_tops=sorted_tops,
            pinned_notes=pinned_notes,
            pinned_ids=pinned_ids,
            total_top_level=total_top_level,
            root_id=root.id if root else None,
            root_sort_by=root_sort_by,
            root_sort_dir=root_sort_dir,
            all_prop_values=collect_prop_values(),
            templates=all_templates(),
            saved_searches=all_saved_searches(),
            all_notes=Note.query.all(),
            notes=[],
            current=None,
            current_parent=None,
            current_sort='modified',
            current_sort_dir='asc',
            search_q='', prop_key='', prop_val='',
            due_date='', due_op='',
            query_suffix='', search_active=False,
            results=[], group_by='',
            parent_notes=get_parent_notes(),
            status_values=[],
            exclude_status='',
        )        

    @app.route('/settings/test-ntfy', methods=['POST'])
    def test_ntfy():
        import requests as req_lib
        data = request.get_json() or {}
        url = (data.get('url') or '').strip()
        if not url:
            return jsonify({'ok': False, 'error': 'empty url'})
        try:
            r = req_lib.post(
                url,
                data='✅ Test from Notes app'.encode('utf-8'),
                headers={
                    'Title': 'Test notification',
                    'Priority': 'default',
                    'Tags': 'white_check_mark',
                },
                timeout=5,
            )
            if r.status_code < 400:
                return jsonify({'ok': True, 'status': r.status_code})
            return jsonify({'ok': False, 'error': f'HTTP {r.status_code}'})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)})

    @app.route('/search/save', methods=['POST'])
    def search_save():
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        params = (data.get('params') or '').strip()
        sort_by = data.get('sort_by', 'modified')
        sort_dir = data.get('sort_dir', 'desc')
        view_mode = data.get('view_mode', 'list')
        if not name:
            return jsonify({'ok': False, 'error': 'empty name'}), 400
        if not sort_by: sort_by = 'modified'
        if sort_dir not in DIR_OPTIONS: sort_dir = 'desc'
        if view_mode not in ('list', 'table'): view_mode = 'list'
        s = SavedSearch(name=name, params=params,
                        sort_by=sort_by, sort_dir=sort_dir,
                        view_mode=view_mode)
        db.session.add(s)
        db.session.commit()
        return jsonify({
            'ok': True, 'id': s.id, 'name': s.name,
            'params': s.params, 'sort_by': s.sort_by, 'sort_dir': s.sort_dir,
            'view_mode': s.view_mode,
        })

    @app.route('/search/<int:search_id>/update', methods=['POST'])
    def search_update(search_id):
        s = SavedSearch.query.get_or_404(search_id)
        data = request.get_json() or {}
        new_params = (data.get('params') or '').strip()
        sort_by = data.get('sort_by', 'modified')
        sort_dir = data.get('sort_dir', 'desc')
        view_mode = data.get('view_mode', 'list')
        if not sort_by: sort_by = 'modified'
        if sort_dir not in DIR_OPTIONS: sort_dir = 'desc'
        if view_mode not in ('list', 'table'): view_mode = 'list'

        s.params = new_params
        s.sort_by = sort_by
        s.sort_dir = sort_dir
        s.view_mode = view_mode
        db.session.commit()
        return jsonify({
            'ok': True, 'id': s.id, 'name': s.name,
            'params': s.params, 'sort_by': s.sort_by, 'sort_dir': s.sort_dir,
            'view_mode': s.view_mode,
        })

    @app.route('/search/<int:search_id>/delete', methods=['POST'])
    def search_delete(search_id):
        s = SavedSearch.query.get_or_404(search_id)
        db.session.delete(s)
        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/search/<int:search_id>/rename', methods=['POST'])
    def search_rename(search_id):
        s = SavedSearch.query.get_or_404(search_id)
        data = request.get_json() or {}
        nv = (data.get('name') or '').strip()
        if not nv:
            return jsonify({'ok': False, 'error': 'empty name'}), 400
        s.name = nv
        db.session.commit()
        return jsonify({'ok': True, 'name': s.name})        

    @app.route('/api/notes/verify-tabs', methods=['POST'])
    def verify_tabs():
        data = request.get_json() or {}
        ids = data.get('ids') or []
        try:
            ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid ids'}), 400
        existing = Note.query.filter(Note.id.in_(ids)).with_entities(Note.id).all()
        return jsonify({'ok': True, 'valid_ids': [r[0] for r in existing]})

    @app.route('/api/searches')
    def api_searches():
        q = request.args.get('q', '').strip()
        limit = request.args.get('limit', 10, type=int)
        query = SavedSearch.query
        if q:
            clean_q = strip_accents(q)
            query = query.filter(
                db.func.STRIP_ACCENTS(SavedSearch.name).contains(clean_q)
            )
        rows = query.order_by(SavedSearch.name).limit(limit).all()
        return jsonify([
            {
                'id': s.id,
                'name': s.name,
                'params': s.params or '',
                'sort_by': s.sort_by or 'modified',
                'sort_dir': s.sort_dir or 'desc',
                'view_mode': s.view_mode or 'list',
            }
            for s in rows
        ])
        
    @app.route('/api/search-blocks')
    def api_search_blocks():
        q = request.args.get('q', '').strip()
        if not q:
            return jsonify([])

        # Use the same accent-stripping logic
        clean_q = strip_accents(q)
        
        # Find notes whose content matches
        results = []
        notes = Note.query.filter(
            db.func.STRIP_ACCENTS(Note.content).contains(clean_q),
            Note.deleted_at.is_(None)
        ).limit(20).all()

        for n in notes:
            blocks = block_utils.split_blocks(n.content or '')
            for b in blocks:
                if clean_q in strip_accents(b['text']):
                    # Truncate text for the UI
                    preview = b['text'].replace('\n', ' ')
                    if len(preview) > 70:
                        preview = preview[:70] + '...'
                    
                    results.append({
                        'note_id': n.id,
                        'note_title': n.title,
                        'block_id': b['id'],
                        'block_text': preview,
                        'full_text': b['text']
                    })
                    if len(results) >= 20:
                        break
            if len(results) >= 20:
                break

        return jsonify(results)     
        
    @app.route('/api/quick-switcher')
    def api_quick_switcher():
        q = request.args.get('q', '').strip()

        # ── Notes ────────────────────────────────────────────────────
        # Use the in-memory index instead of a DB scan, so typing is fast
        # and we can also serve an empty query (recents) cheaply.
        index = get_quick_switcher_index()

        if not q:
            # Empty query: return the 5 most recently modified notes.
            recent = sorted(
                index, key=lambda e: e['updated_at_ts'], reverse=True
            )[:5]
            notes_out = [{'id': e['id'], 'title': e['title']} for e in recent]
        else:
            clean_q = strip_accents(q)
            scored = []
            for e in index:
                # fuzzy_score() re-normalizes internally; use the raw title.
                s = fuzzy_score(q, e['title'])
                if s > 0:
                    scored.append((s, e['updated_at_ts'], e))
            # Higher score first; tiebreaker = newer first.
            scored.sort(key=lambda x: (-x[0], -x[1]))
            notes_out = [
                {'id': e['id'], 'title': e['title']}
                for _, _, e in scored[:20]
            ]

        # ── Saved searches ───────────────────────────────────────────
        if not q:
            searches_out = []
        else:
            clean_q = strip_accents(q)
            searches = (
                SavedSearch.query
                .filter(db.func.STRIP_ACCENTS(SavedSearch.name).contains(clean_q))
                .order_by(SavedSearch.name)
                .limit(10)
                .all()
            )
            searches_out = [{
                'id': s.id, 'name': s.name, 'params': s.params or '',
                'sort_by': s.sort_by or 'modified', 'sort_dir': s.sort_dir or 'desc',
                'view_mode': s.view_mode or 'list',
            } for s in searches]

        # ── Blocks ───────────────────────────────────────────────────
        blocks_out = []
        if q and len(q) >= 2:
            clean_q = strip_accents(q)
            matching_notes = Note.query.filter(
                db.func.STRIP_ACCENTS(Note.content).contains(clean_q),
                Note.deleted_at.is_(None)
            ).limit(5).all()

            for n in matching_notes:
                blocks = block_utils.split_blocks(n.content or '')
                for b in blocks:
                    if clean_q in strip_accents(b['text']):
                        preview = b['text'].replace('\n', ' ')
                        if len(preview) > 70:
                            preview = preview[:70] + '...'
                        blocks_out.append({
                            'note_id': n.id,
                            'note_title': n.title,
                            'block_id': b['id'],
                            'block_text': preview,
                            'full_text': b['text']
                        })
                        if len(blocks_out) >= 20:
                            break
                if len(blocks_out) >= 20:
                    break

        return jsonify({
            'notes': notes_out,
            'searches': searches_out,
            'blocks': blocks_out
        })      

    @app.route('/sort', methods=['POST'])
    def set_sort():
        data = request.get_json() or {}
        sort_by = data.get('sort_by', 'modified')
        sort_dir = data.get('sort_dir', 'asc')
        if sort_by not in SORT_OPTIONS: sort_by = 'modified'
        if sort_dir not in DIR_OPTIONS: sort_dir = 'asc'
        source_id = data.get('source_id')

        if source_id:
            p = Note.query.get(int(source_id))
            if p:
                p.sort_by = sort_by
                p.sort_dir = sort_dir
                db.session.commit()
            invalidate_parent_notes_cache()
            return jsonify({'ok': True})

        Setting.set('root_sort_by', sort_by)
        Setting.set('root_sort_dir', sort_dir)
        invalidate_parent_notes_cache()
        return jsonify({'ok': True})

    @app.route('/api/pinned', methods=['GET'])
    def api_get_pinned():
        current = Setting.get('pinned_note_ids', '')
        ids = [int(x) for x in current.split(',') if x.isdigit()]

        pinned = []
        if ids:
            notes = Note.query.filter(Note.id.in_(ids)).all()
            pinned = [{'id': n.id, 'title': n.title} for n in notes]

        return jsonify({'ok': True, 'pinned': pinned})

    @app.route('/api/pinned', methods=['POST'])
    def api_set_pinned():
        data = request.get_json() or {}
        note_id = data.get('id')

        current = Setting.get('pinned_note_ids', '')
        ids = [x for x in current.split(',') if x.isdigit()]

        if note_id:
            note_id = str(note_id)
            if note_id not in ids:
                ids.append(note_id)

        Setting.set('pinned_note_ids', ','.join(ids))
        invalidate_parent_notes_cache()
        return jsonify({'ok': True})

    @app.route('/api/pinned/unpin/<int:note_id>', methods=['POST'])
    def api_unpin(note_id):
        current = Setting.get('pinned_note_ids', '')
        ids = [x for x in current.split(',') if x.isdigit()]
        ids = [i for i in ids if i != str(note_id)]
        Setting.set('pinned_note_ids', ','.join(ids))
        invalidate_parent_notes_cache()
        return jsonify({'ok': True})

    @app.route('/templates')
    def templates_list():
        return jsonify([
            {'id': n.id, 'title': n.title}
            for n in all_templates()
        ])

    @app.route('/note/<int:note_id>/toggle-template', methods=['POST'])
    def note_toggle_template(note_id):
        note = Note.query.get_or_404(note_id)
        note.is_template = not bool(note.is_template)
        db.session.commit()
        # Tell the server-side cache to rebuild so the Templates folder updates
        invalidate_parent_notes_cache()
        return jsonify({'ok': True, 'is_template': note.is_template})

    @app.route('/note/<int:note_id>/toggle-blank-title', methods=['POST'])
    def note_toggle_blank_title(note_id):
        note = Note.query.get_or_404(note_id)
        note.blank_title_on_use = not bool(note.blank_title_on_use)
        db.session.commit()
        return jsonify({'ok': True, 'blank_title_on_use': note.blank_title_on_use})

    @app.route('/note/new', methods=['POST'])
    def note_new():
        root = get_root_note()
        parent_id = request.form.get('parent_id', type=int)
        template_id = request.form.get('template_id', type=int)

        if parent_id is None and root:
            parent_id = root.id

        if not template_id:
            default_tpl = Setting.get('default_template_id', '')
            if default_tpl and default_tpl.isdigit():
                template_id = int(default_tpl)

        tpl = Note.query.get(template_id) if template_id else None

        if tpl:
            if tpl.blank_title_on_use:
                title = ''
            else:
                title = substitute_vars(tpl.title or 'Untitled')
            content = substitute_vars(tpl.content or '')
            properties = {
                k: substitute_vars(str(v))
                for k, v in (tpl.properties or {}).items()
            }
        else:
            title = ''
            content = ''
            properties = {}

        note = Note(title=title, slug='untitled',
                    parent_id=parent_id, is_template=False,
                    content=content, properties=properties)

        db.session.add(note)
        db.session.commit()
        base = slugify(note.title) or 'untitled'
        note.slug = f"{base}-{note.id}"
        db.session.commit()
        # Only invalidate prop cache if this new note added properties.
        if note.properties:
            invalidate_prop_cache()
        invalidate_parent_notes_cache()
        import time
        t = int(time.time() * 1000)
        return redirect(url_for('index', note=note.id, parent=parent_id, focus='title', t=t))

    @app.route('/note/create', methods=['POST'])
    def note_create():
        """Create a note with title/content directly (for CLI use)."""
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '')
        parent_id = request.form.get('parent_id', type=int)

        root = get_root_note()
        if parent_id is None and root:
            parent_id = root.id

        note = Note(
            title=title,
            slug='untitled',
            content=content,
            properties={},
            parent_id=parent_id,
            is_template=False,
        )
        db.session.add(note)
        db.session.commit()
        base = slugify(note.title) or 'untitled'
        note.slug = f"{base}-{note.id}"
        db.session.commit()

        return jsonify({'ok': True, 'id': note.id, 'title': note.title})

    @app.route('/note/quick', methods=['POST'])
    def note_quick():
        parent_id = Setting.get('quick_note_parent_id', '').strip()

        parent_note = None
        if parent_id.isdigit():
            parent_note = Note.query.get(int(parent_id))

        if not parent_note:
            parent_note = get_root_note()

        note = Note(
            title='',
            slug='untitled',
            content='',
            properties={},
            parent_id=parent_note.id if parent_note else None,
            is_template=False,
        )
        db.session.add(note)
        db.session.commit()

        base = slugify(note.title) or 'untitled'
        note.slug = f"{base}-{note.id}"
        db.session.commit()
        # A quick note is always blank → its properties can't change the
        # cached property-value set. Only invalidate the parent cache.
        invalidate_parent_notes_cache()

        import time
        t = int(time.time() * 1000)
        return jsonify({
            'ok': True,
            'id': note.id,
            'url': url_for('index', note=note.id, focus='title', t=t) + '&newtab=1'
        })
        
    @app.route('/note/new-map', methods=['POST'])
    def note_new_map():
        """Create a new map note in the configured folder, and redirect to it."""
        parent_id = None

        # Prefer the explicitly configured map-note parent
        stored = Setting.get('map_note_parent_id', '').strip()
        if stored.isdigit():
            candidate = Note.query.get(int(stored))
            if candidate and candidate.deleted_at is None:
                parent_id = candidate.id

        # Fall back to Root
        if parent_id is None:
            root = get_root_note()
            parent_id = root.id if root else None

        now = datetime.now()
        title = now.strftime('Map %Y-%m-%d %H-%M')

        note = Note(
            title=title,
            slug='untitled',
            content='',
            properties={},
            parent_id=parent_id,
            is_template=False,
            is_map_note=True,
        )
        db.session.add(note)
        db.session.commit()
        note.slug = f"{slugify(note.title) or 'map'}-{note.id}"
        db.session.commit()

        # Invalidate caches: a new note changes the sidebar tree and the
        # property index (empty, but still).
        invalidate_parent_notes_cache()

        import time
        t = int(time.time() * 1000)
        return jsonify({
            'ok': True,
            'id': note.id,
            'url': url_for('index', note=note.id, focus='title', t=t),
        })
        
    @app.route('/api/note/<int:parent_id>/add-waypoint', methods=['POST'])
    def note_add_waypoint(parent_id):
        """Create a child note under a map note, carrying lat/lng properties."""
        parent = Note.query.get_or_404(parent_id)
        if not parent.is_map_note:
            return jsonify({'ok': False, 'error': 'parent is not a map note'}), 400

        data = request.get_json() or {}
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'ok': False, 'error': 'empty title'}), 400
        lat = data.get('lat')
        lng = data.get('lng')
        if lat is None or lng is None:
            return jsonify({'ok': False, 'error': 'missing coordinates'}), 400
        try:
            lat = float(lat)
            lng = float(lng)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid coordinates'}), 400

        child = Note(
            title=title,
            slug='untitled',
            content='',
            properties={
                'latitude': f'{lat:.6f}',
                'longitude': f'{lng:.6f}',
            },
            parent_id=parent.id,
            is_template=False,
        )
        db.session.add(child)
        db.session.commit()
        child.slug = f"{slugify(child.title) or 'point'}-{child.id}"
        db.session.commit()

        invalidate_prop_cache()
        invalidate_parent_notes_cache()

        return jsonify({
            'ok': True,
            'id': child.id,
            'title': child.title,
            'lat': lat,
            'lng': lng,
        })        

    @app.route('/note/<int:note_id>/save', methods=['POST'])
    def note_save(note_id):
        note = Note.query.get_or_404(note_id)
        if request.is_json:
            data = request.get_json() or {}
        else:
            try:
                data = json.loads(request.get_data(as_text=True) or '{}')
            except Exception:
                data = {}

        client_ts = data.get('_client_updated_at')
        if note.updated_at:
            if not client_ts:
                return jsonify({
                    'ok': False,
                    'error': 'missing_client_timestamp',
                    'conflict': True,
                    'updated_at': note.updated_at.isoformat() + 'Z',
                }), 409
            try:
                client_dt = datetime.fromisoformat(
                    str(client_ts).replace('Z', '+00:00')
                )
                if client_dt.tzinfo:
                    client_dt = client_dt.astimezone(timezone.utc).replace(tzinfo=None)
                if client_dt < note.updated_at:
                    return jsonify({
                        'ok': False,
                        'error': 'stale',
                        'conflict': True,
                        'updated_at': note.updated_at.isoformat() + 'Z',
                    }), 409
            except (ValueError, TypeError):
                return jsonify({
                    'ok': False,
                    'error': 'invalid_client_timestamp',
                    'conflict': True,
                    'updated_at': note.updated_at.isoformat() + 'Z',
                }), 409

        new_title = data.get('title')
        if new_title is None:
            new_title = ''
        new_title = str(new_title).strip()

        is_unloading = bool(data.get('_is_unloading', False))

        title_changed = False
        if new_title:
            if new_title != note.title:
                note.title = new_title
                title_changed = True
        elif is_unloading:
            if not note.title or note.title.strip().lower() == 'untitled' \
                    or note.title.startswith('Note '):
                note.title = datetime.now().strftime('Note %Y-%m-%d %H-%M')
                title_changed = True

        if title_changed:
            note.slug = f"{slugify(note.title) or 'untitled'}-{note.id}"

        note.content = data.get('content', '')

        # ─── Handle "set/clear reminder on a block" ──────────────────
        # Client sends: block_reminder = { text, due_at: 'ISO'|null }
        #
        # Steps:
        #   1. Find the block in the content by exact text match.
        #   2. If it already has a marker, use that ID.
        #   3. Otherwise, generate a new ID and splice the marker in.
        #   4. VERIFY the ID is actually present in the content
        #      afterwards. If it isn't, the splice silently failed
        #      (usually because the client's `text` didn't exactly
        #      match any block — e.g. trailing whitespace, \r\n vs
        #      \n, etc.). In that case we must NOT create a BlockMeta
        #      row, or the reminder becomes an orphan that never fires.
        _block_reminder = data.get('block_reminder')
        _reminder_block_id = None
        _reminder_failed = None
        if isinstance(_block_reminder, dict):
            _btext = (_block_reminder.get('text') or '').strip()
            if _btext:
                _existing = None
                for _b in block_utils.split_blocks(note.content):
                    if _b['text'] == _btext and _b['marker']:
                        _existing = _b['id']
                        break

                if _existing:
                    _reminder_block_id = _existing
                else:
                    _candidate = block_utils.new_block_id()
                    _new_content = block_utils.add_marker_to_first_match(
                        note.content, _btext, _candidate
                    )
                    # Only commit if the marker actually made it into
                    # the content. If not, the block wasn't found by
                    # exact text match — bail out cleanly.
                    if _candidate in block_utils.markers_in(_new_content):
                        note.content = _new_content
                        _reminder_block_id = _candidate
                    else:
                        _reminder_failed = (
                            'Could not attach reminder: the block text '
                            'did not match any block in this note. Try '
                            'reloading the note and setting the '
                            'reminder again.'
                        )

        # Handle "delete block" requests.
        # Client sends: block_delete = { text, block_id }
        _block_delete = data.get('block_delete')
        _delete_block_id = None
        if isinstance(_block_delete, dict):
            _del_text = (_block_delete.get('text') or '').strip()
            _del_id = _block_delete.get('block_id') or None
            if _del_text or _del_id:
                _blocks = block_utils.split_blocks(note.content)
                _target = None
                for _b in _blocks:
                    if _del_id and _b['id'] == _del_id:
                        _target = _b
                        break
                    if _del_text and _b['text'] == _del_text:
                        _target = _b
                        break
                if _target:
                    _before = note.content[: _target['start']]
                    _after = note.content[_target['end'] :]
                    _joined = _before + _after
                    _joined = re.sub(r'\n{3,}', '\n\n', _joined)
                    _joined = _joined.strip('\n')
                    note.content = _joined
                    _delete_block_id = _target['id']

        # For the client's mid-session dropdown refresh (POST /api/prop-values):
        # did this save change the note's properties at all?
        _old_props_for_refresh = dict(note.properties or {})
        note.properties = sort_props_status_last(normalize_props(data.get('properties') or {}))
        _new_props_for_refresh = dict(note.properties or {})
        properties_changed = (_old_props_for_refresh != _new_props_for_refresh)

        due = data.get('due_at')
        if due:
            due = str(due).replace('Z', '+00:00')
            try:
                dt = datetime.fromisoformat(due)
            except ValueError:
                dt = None
            if dt:
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                if note.due_at != dt:
                    note.notified = False
                note.due_at = dt
        else:
            if note.due_at is not None:
                note.notified = False
            note.due_at = None

        link_ids = data.get('link_ids') or []
        outgoing = set(Note.query.filter(Note.id.in_(link_ids)).all()) if link_ids else set()

        for t in re.findall(r'\[\[([^\]]+)\]\]', note.content or ''):
            target = Note.query.filter(Note.title.ilike(t.strip())).first()
            if target and target.id != note.id:
                outgoing.add(target)

        note.outbound = list(outgoing)
        db.session.commit()

        import time
        Setting.set(f'last_change_{note.id}', str(int(time.time() * 1000)))
        Setting.set(f'last_change_client_{note.id}', str(data.get('_client_id', '')))

        invalidate_prop_cache()
        invalidate_parent_notes_cache()

        # ─── Sync block_meta rows with the current content ────────────
        try:
            new_content = note.content or ''

            # Apply client-provided updates (if any)
            incoming = dict(data.get('block_meta_updates') or {})
            if _reminder_block_id:
                incoming[_reminder_block_id] = {
                    'due_at': (_block_reminder or {}).get('due_at'),
                }
            for bid, patch in incoming.items():
                if not isinstance(patch, dict):
                    continue
                row = BlockMeta.query.filter_by(note_id=note.id, block_id=bid).first()
                if row is None:
                    row = BlockMeta(note_id=note.id, block_id=bid)
                    db.session.add(row)
                if 'due_at' in patch:
                    raw = patch.get('due_at')
                    if raw:
                        try:
                            dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
                            if dt.tzinfo is not None:
                                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                            row.due_at = dt
                            row.reminder_sent = False
                        except (ValueError, TypeError):
                            pass
                    else:
                        row.due_at = None
                        row.reminder_sent = False

            # Backfill: any marker in the content without a row yet
            # gets a bare row. Self-healing across lost rows.
            markers = block_utils.markers_in(new_content)
            if markers:
                existing_ids = {
                    r.block_id for r in
                    BlockMeta.query.filter_by(note_id=note.id).all()
                }
                for bid in markers:
                    if bid not in existing_ids:
                        db.session.add(BlockMeta(note_id=note.id, block_id=bid))
            # Explicit block deletion — remove the metadata row
            if _delete_block_id:
                _del_row = BlockMeta.query.filter_by(
                    note_id=note.id, block_id=_delete_block_id
                ).first()
                if _del_row:
                    db.session.delete(_del_row)
            db.session.commit()
        except Exception as _e:
            print(f"[block_meta sync] {_e}")
            db.session.rollback()

        try:
            _blk_count = BlockMeta.query.filter_by(note_id=note.id).count()
        except Exception:
            _blk_count = 0

        response = {
            'ok': True,
            'slug': note.slug,
            'due_at': note.due_at.isoformat() + 'Z' if note.due_at else None,
            'updated_at': note.updated_at.isoformat() + 'Z' if note.updated_at else '',
            'properties_changed': properties_changed,
            'blocks_count': _blk_count,
            'block_id': _reminder_block_id,
            'block_deleted': _delete_block_id,
        }
        if _reminder_failed:
            response['ok'] = False
            response['error'] = _reminder_failed
            response['block_reminder_failed'] = True
        return jsonify(response)
        
    @app.route('/api/note/<int:note_id>/update-property', methods=['POST'])
    def update_note_property(note_id):
        note = Note.query.get_or_404(note_id)
        data = request.get_json() or {}
        
        key = str(data.get('key', '')).strip().lower()
        val = str(data.get('value', '')).strip()
        
        if not key:
            return jsonify({'ok': False, 'error': 'missing key'}), 400
            
        props = dict(note.properties or {})
        
        if val:
            props[key] = val
        else:
            # If the value is empty, remove the property entirely
            props.pop(key, None)
            
        note.properties = sort_props_status_last(props)
        
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(note, "properties")
        
        db.session.commit()
        
        # Invalidate caches so the sidebar and search update
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        
        # Broadcast change for the live-polling
        import time
        Setting.set(f'last_change_{note_id}', str(int(time.time() * 1000)))
        Setting.set(f'last_change_client_{note_id}', 'table-edit')

        return jsonify({'ok': True})        

    @app.route('/note/<int:note_id>/rename', methods=['POST'])
    def note_rename(note_id):
        note = Note.query.get_or_404(note_id)
        data = request.get_json() or {}
        new_title = (data.get('title') or '').strip()
        if not new_title:
            return jsonify({'ok': False, 'error': 'empty title'}), 400
        note.title = new_title
        note.slug = f"{slugify(new_title)}-{note.id}"
        db.session.commit()
        return jsonify({'ok': True, 'title': note.title})

    @app.route('/note/<int:note_id>/move', methods=['POST'])
    def note_move(note_id):
        note = Note.query.get_or_404(note_id)
        target = (request.json or {}).get('parent_id')

        root = get_root_note()
        if note.id == (root.id if root else None):
            return jsonify({'ok': False, 'error': 'cannot move root'}), 400

        if target:
            target_note = Note.query.get(int(target))
            if not target_note or target_note.id == note.id:
                return jsonify({'ok': False, 'error': 'invalid parent'}), 400
            n = target_note
            seen = set()
            while n and n.id not in seen:
                seen.add(n.id)
                if n.id == note.id:
                    return jsonify({'ok': False, 'error': 'cycle'}), 400
                n = n.parent

        if not target and root:
            target = root.id

        note.parent_id = int(target) if target else None
        db.session.commit()
        invalidate_parent_notes_cache()
        return jsonify({'ok': True})

    @app.route('/api/note/<int:note_id>/make-subtask', methods=['POST'])
    def make_subtask(note_id):
        note = Note.query.get_or_404(note_id)
        data = request.get_json() or {}
        target_parent_id = data.get('parent_id')
        
        # Treat 0 as "move to Root"
        if target_parent_id == 0 or target_parent_id is None:
            root = get_root_note()
            if not root:
                return jsonify({'ok': False, 'error': 'Root not found'}), 404
            target_parent = root
        else:
            target_parent = Note.query.get(int(target_parent_id))
            if not target_parent:
                return jsonify({'ok': False, 'error': 'Parent not found'}), 404
            
        # Prevent moving a note under itself or its descendants
        n = target_parent
        while n:
            if n.id == note.id:
                return jsonify({'ok': False, 'error': 'Cannot move note under itself'}), 400
            n = n.parent

        # 1. Move the note
        note.parent_id = target_parent.id
        
        # 2. Find the Task template from settings
        tpl_id = Setting.get('task_template_id', '')
        task_tpl = None
        if tpl_id and tpl_id.isdigit():
            task_tpl = Note.query.get(int(tpl_id))
        
        if not task_tpl:
            task_tpl = Note.query.filter(Note.is_template == True, Note.title.ilike('%task%')).first()
        
        merged_props = dict(note.properties or {})
        if task_tpl:
            tpl_props = task_tpl.properties or {}
            for k, v in tpl_props.items():
                key = str(k).strip().lower()
                # Only add if missing (don't override existing values)
                if key not in merged_props or not str(merged_props[key]).strip():
                    merged_props[key] = v
                    
        note.properties = sort_props_status_last(merged_props)
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(note, "properties")
        
        db.session.commit()
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        
        return jsonify({'ok': True})

    @app.route('/api/note/create-task', methods=['POST'])
    def create_task():
        data = request.get_json() or {}
        parent_id = data.get('parent_id')
        
        parent = Note.query.get(int(parent_id)) if parent_id else get_root_note()
        if not parent:
            return jsonify({'ok': False, 'error': 'Parent not found'}), 404

        # Find the Task template from settings
        tpl_id = Setting.get('task_template_id', '')
        task_tpl = None
        if tpl_id and tpl_id.isdigit():
            task_tpl = Note.query.get(int(tpl_id))
        
        # Fallback to searching by name if no setting exists
        if not task_tpl:
            task_tpl = Note.query.filter(Note.is_template == True, Note.title.ilike('%task%')).first()
        
        new_content = ''
        new_props = {'category': 'tasks'} # Default fallback
        
        if task_tpl:
            new_content = task_tpl.content or ''
            new_props = {}
            for k, v in (task_tpl.properties or {}).items():
                new_props[str(k).strip().lower()] = substitute_vars(str(v))
            
            # Ensure category includes 'tasks'
            cat = new_props.get('category', '')
            if 'tasks' not in cat.lower():
                new_props['category'] = (cat + ', tasks').strip(', ') if cat else 'tasks'
        else:
            new_props = {'category': 'tasks'}

        # Create the note
        new_note = Note(
            title='',
            slug='untitled',
            content=new_content,
            properties=sort_props_status_last(new_props),
            parent_id=parent.id,
            is_template=False
        )
        db.session.add(new_note)
        db.session.commit()
        new_note.slug = f"{slugify(new_note.title) or 'task'}-{new_note.id}"
        db.session.commit()
        
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        
        return jsonify({'ok': True, 'id': new_note.id})        
        
    @app.route('/note/<int:note_id>/delete', methods=['POST'])
    def note_delete(note_id):
        note = Note.query.get_or_404(note_id)
        root = get_root_note()
        if root and note.id == root.id:
            return jsonify({'ok': False, 'error': 'cannot delete root'}), 400
        
        # Soft delete the note and all descendants
        def mark_deleted(n):
            n.deleted_at = datetime.utcnow()
            for child in n.children:
                mark_deleted(child)
                
        mark_deleted(note)
        db.session.commit()
        
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        
        return jsonify({'ok': True})

    @app.route('/notes/bulk-delete', methods=['POST'])
    def notes_bulk_delete():
        data = request.get_json() or {}
        ids = data.get('ids') or []
        try:
            ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid ids'}), 400
        if not ids:
            return jsonify({'ok': True, 'deleted': []})

        root = get_root_note()
        root_id = root.id if root else None
        ids = [i for i in ids if i != root_id]

        to_delete = set()
        def add_descendants(note):
            if note is None or note.id in to_delete:
                return
            to_delete.add(note.id)
            for c in note.children:
                add_descendants(c)

        for nid in ids:
            n = Note.query.get(nid)
            if n:
                add_descendants(n)

        deleted_ids = []
        now = datetime.utcnow()
        for nid in to_delete:
            n = Note.query.get(nid)
            if n:
                n.deleted_at = now
                deleted_ids.append(nid)
                
        db.session.commit()
        
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        
        return jsonify({'ok': True, 'deleted': deleted_ids})
        
    @app.route('/trash')
    def trash():
        from sqlalchemy import desc
        # Find all deleted notes that are top-level in the trash (their parent is not deleted)
        all_deleted = Note.query.filter(Note.deleted_at.isnot(None)).order_by(desc(Note.deleted_at)).all()
        top_deleted = [n for n in all_deleted if not n.parent or n.parent.deleted_at is None]
        
        root = get_root_note()
        root_sort_by = Setting.get('root_sort_by', 'modified')
        root_sort_dir = Setting.get('root_sort_dir', 'asc')
        sorted_tops, total_top_level = get_top_level_children(root, root_sort_by, root_sort_dir)
        
        pinned_ids_str = Setting.get('pinned_note_ids', '')
        pinned_ids = [int(x) for x in pinned_ids_str.split(',') if x.isdigit()]
        pinned_notes = Note.query.filter(Note.id.in_(pinned_ids)).order_by(Note.title.asc()).all() if pinned_ids else []

        return render_template(
            'trash.html',
            deleted_notes=top_deleted,
            sorted_tops=sorted_tops,
            pinned_notes=pinned_notes,
            pinned_ids=pinned_ids,
            total_top_level=total_top_level,
            root_id=root.id if root else None,
            root_sort_by=root_sort_by,
            root_sort_dir=root_sort_dir,
            all_prop_values=collect_prop_values(),
            templates=all_templates(),
            saved_searches=all_saved_searches(),
            all_notes=Note.query.all(),
            notes=[],
            current=None,
            current_parent=None,
            current_sort='modified',
            current_sort_dir='asc',
            search_q='', prop_key='', prop_val='',
            due_date='', due_op='',
            query_suffix='', search_active=False,
            results=[], group_by='',
            parent_notes=get_parent_notes(),
            status_values=[],
            exclude_status='',
            outline_view_mode=Setting.get('outline_view_mode', 'card'),
        )
        
    @app.route('/trash/empty', methods=['POST'])
    def trash_empty():
        # Find all notes marked as deleted
        deleted_notes = Note.query.filter(Note.deleted_at.isnot(None)).all()
        
        # Permanently delete them (and their attachments from disk)
        for n in deleted_notes:
            for att in list(n.attachments):
                try:
                    os.remove(os.path.join(app.config['UPLOAD_FOLDER'], att.filename))
                except OSError:
                    pass
            db.session.delete(n)
            
        db.session.commit()
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        return jsonify({'ok': True})        

    @app.route('/note/<int:note_id>/restore', methods=['POST'])
    def note_restore(note_id):
        note = Note.query.get_or_404(note_id)
        
        def restore(n):
            n.deleted_at = None
            for child in n.children:
                restore(child)
                
        restore(note)
        db.session.commit()
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        return jsonify({'ok': True})        

    @app.route('/notes/bulk-move', methods=['POST'])
    def notes_bulk_move():
        data = request.get_json() or {}
        ids = data.get('ids') or []
        try:
            ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid ids'}), 400
        if not ids:
            return jsonify({'ok': True, 'moved': []})

        target = data.get('parent_id')
        target_id = int(target) if target not in (None, '', 0) else None

        root = get_root_note()
        root_id = root.id if root else None

        ids = [i for i in ids if i != root_id]
        if not ids:
            return jsonify({'ok': True, 'moved': []})

        target_note = None
        if target_id is not None:
            target_note = Note.query.get(target_id)
            if not target_note:
                return jsonify({'ok': False, 'error': 'target not found'}), 400

        if target_note:
            move_set = set(ids)
            n = target_note
            seen = set()
            while n and n.id not in seen:
                seen.add(n.id)
                if n.id in move_set:
                    return jsonify({'ok': False, 'error': 'cycle'}), 400
                n = n.parent

        effective_parent = target_id
        if effective_parent is None and root:
            effective_parent = root.id

        moved = []
        for nid in ids:
            note = Note.query.get(nid)
            if not note:
                continue
            note.parent_id = effective_parent
            moved.append(nid)
        db.session.commit()
        invalidate_parent_notes_cache()
        return jsonify({'ok': True, 'moved': moved})
        
    @app.route('/note/<int:note_id>/children')
    def note_children(note_id):
        db.session.expire_all()
        note = Note.query.get_or_404(note_id)
        
        # Sync sorting with Root settings if this is Root
        root = get_root_note()
        if root and note.id == root.id:
            sort_by = Setting.get('root_sort_by', 'modified')
            sort_dir = Setting.get('root_sort_dir', 'asc')
        else:
            sort_by = note.sort_by or 'modified'
            sort_dir = note.sort_dir or 'asc'
            
        if sort_by not in SORT_OPTIONS: sort_by = 'modified'
        if sort_dir not in DIR_OPTIONS: sort_dir = 'asc'

        # 1. Filter out deleted children
        active_children = [c for c in note.children if c.deleted_at is None]

        # 2. Calculate has_children for each child BEFORE sorting
        for c in active_children:
            c.has_children = len([child for child in c.children if child.deleted_at is None]) > 0

        # 3. NOW sort them
        children = _sorted_children(
            active_children,
            sort_by,
            sort_dir,
        )
        
        out = []
        for c in children:
            out.append({
                'id': c.id,
                'title': c.title,
                'has_children': c.has_children,
                'is_template': bool(c.is_template),
                'is_map_note': bool(c.is_map_note),
                'properties': c.properties or {},
                'preview': _child_preview(c.content, 140),
                'content': c.content or '',
            })

        # Inject Virtual Notes into Root's outline too
        if root and note.id == root.id:
            pinned_ids_str = Setting.get('pinned_note_ids', '')
            pinned_ids = [int(x) for x in pinned_ids_str.split(',') if x.isdigit()]
            has_pinned = bool(pinned_ids)

            virtual_out = []
            if has_pinned:
                virtual_out.append({
                    'id': 'pinned-section', 'title': 'Pinned', 'has_children': True,
                    'is_template': False, 'is_map_note': False, 'properties': {}, 'preview': '', 'content': ''
                })
            virtual_out.append({
                'id': 'recent-section', 'title': 'Recent', 'has_children': True,
                'is_template': False, 'is_map_note': False, 'properties': {}, 'preview': '', 'content': ''
            })
            from models import SavedSearch
            if SavedSearch.query.first():
                virtual_out.append({
                    'id': 'saved-search-section', 'title': 'Searches', 'has_children': True,
                    'is_template': False, 'is_map_note': False, 'properties': {}, 'preview': '', 'content': ''
                })

            out = virtual_out + out

        return jsonify({
            'ok': True,
            'sort_by': sort_by,
            'sort_dir': sort_dir,
            'children': out,
        })

    @app.route('/top-level')
    def top_level():
        db.session.expire_all()
        root = get_root_note()
        if not root:
            return jsonify({'ok': True, 'sort_by': 'modified', 'sort_dir': 'asc',
                            'children': []})
        sort_by = Setting.get('root_sort_by', 'modified')
        sort_dir = Setting.get('root_sort_dir', 'asc')
        if sort_by not in SORT_OPTIONS: sort_by = 'modified'
        if sort_dir not in DIR_OPTIONS: sort_dir = 'asc'

        notes, _total = get_top_level_children(root, sort_by, sort_dir)

        # --- INJECT CUSTOM VIRTUAL FOLDERS HERE TOO ---
        pinned_ids_str = Setting.get('pinned_note_ids', '')
        pinned_ids = [int(x) for x in pinned_ids_str.split(',') if x.isdigit()]
        pinned_notes = Note.query.filter(Note.id.in_(pinned_ids)).all() if pinned_ids else []
        
        virtual_notes = []
        if pinned_notes:
            virtual_notes.append(VirtualNote('pinned-section', 'Pinned', True))
        virtual_notes.append(VirtualNote('recent-section', 'Recent', True))
        
        from models import SavedSearch
        if SavedSearch.query.first():
            virtual_notes.append(VirtualNote('saved-search-section', 'Searches', True))

        notes = notes + virtual_notes

        # Re-apply sort with safety checks
        reverse = (sort_dir == 'desc')
        if sort_by == 'title':
            notes.sort(key=lambda n: _note_title_sort_key(n, reverse=reverse), reverse=reverse)
        elif sort_by == 'created':
            notes.sort(key=lambda n: getattr(n, 'created_at', None) or datetime.min, reverse=reverse)
        elif sort_by in ('modified', 'updated', ''):
            notes.sort(key=lambda n: getattr(n, 'updated_at', None) or datetime.min, reverse=reverse)
        # ----------------------------------------------

        out = []
        for n in notes:
            out.append({
                'id': n.id,
                'title': n.title,
                'has_children': getattr(n, 'has_children', True),
                'is_template': bool(getattr(n, 'is_template', False)),
                'is_custom': bool(getattr(n, 'is_custom', False)),
                'preview': _child_preview(getattr(n, 'content', ''), 140),
            })
        return jsonify({
            'ok': True,
            'sort_by': sort_by,
            'sort_dir': sort_dir,
            'children': out,
        })

    @app.route('/daily-note')
    def daily_note():
        journal = _get_journal_parent()
        
        requested_date = request.args.get('date')
        
        if requested_date and re.match(r'^\d{4}-\d{2}-\d{2}$', requested_date):
            try:
                d = datetime.strptime(requested_date, '%Y-%m-%d')
            except ValueError:
                d = datetime.now()
        else:
            d = datetime.now()

        pattern = Setting.get('daily_note_title_template', '{{date}}')
        title = pattern.replace('{{date}}', d.strftime('%Y-%m-%d'))
        title = title.replace('{{year}}', d.strftime('%Y'))
        title = title.replace('{{month}}', d.strftime('%m'))
        title = title.replace('{{day}}', d.strftime('%A'))
        title = title.replace('{{time}}', '')
        title = title.replace('{{datetime}}', d.strftime('%Y-%m-%d'))
        title = title.strip() or d.strftime('%Y-%m-%d')

        child = Note.query.filter(
            Note.parent_id == journal.id,
            Note.title == title,
            Note.deleted_at.is_(None)
        ).order_by(Note.id.asc()).first()
        
        if not child:
            new_content = ''
            new_props = {}
            
            tpl_id = Setting.get('daily_note_template_id', '')
            if tpl_id and tpl_id.isdigit():
                tpl = Note.query.get(int(tpl_id))
                if tpl:
                    new_content = tpl.content or ''
                    new_content = new_content.replace('{{date}}', d.strftime('%Y-%m-%d'))
                    
                    new_props = dict(tpl.properties or {})
                    for k, v in new_props.items():
                        val_str = str(v) if v is not None else ''
                        val_str = val_str.replace('{{date}}', d.strftime('%Y-%m-%d'))
                        new_props[k] = val_str

            child = Note(
                title=title,
                slug='untitled',
                content=new_content,
                properties=sort_props_status_last(new_props),
                parent_id=journal.id,
                is_template=False,
                deleted_at=None,
            )
            db.session.add(child)
            db.session.commit()
            
            child.slug = f"{slugify(child.title) or 'note'}-{child.id}"
            db.session.commit()
            
            invalidate_prop_cache()
            invalidate_parent_notes_cache()

        # --- FIX: Apply template properties if the note has none ---
        if not child.properties:
            tpl_id = Setting.get('daily_note_template_id', '')
            if tpl_id and tpl_id.isdigit():
                tpl = Note.query.get(int(tpl_id))
                if tpl and tpl.properties:
                    merged = {}
                    for k, v in tpl.properties.items():
                        val_str = str(v) if v is not None else ''
                        val_str = val_str.replace('{{date}}', d.strftime('%Y-%m-%d'))
                        merged[k] = val_str
                    child.properties = sort_props_status_last(merged)
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(child, "properties")
                    db.session.commit()
                    invalidate_prop_cache()

        return redirect(url_for('index', note=child.id, nofocus='1'))

    @app.route('/api/daily-note/ensure', methods=['POST'])
    def ensure_daily_note():
        journal = _get_journal_parent()
        d = datetime.now()
        
        pattern = Setting.get('daily_note_title_template', '{{date}}')
        title = pattern.replace('{{date}}', d.strftime('%Y-%m-%d'))
        title = title.replace('{{year}}', d.strftime('%Y'))
        title = title.replace('{{month}}', d.strftime('%m'))
        title = title.replace('{{day}}', d.strftime('%A'))
        title = title.replace('{{time}}', '')
        title = title.replace('{{datetime}}', d.strftime('%Y-%m-%d'))
        title = title.strip() or d.strftime('%Y-%m-%d')

        child = Note.query.filter(
            Note.parent_id == journal.id,
            Note.title == title,
            Note.deleted_at.is_(None)
        ).order_by(Note.id.asc()).first()

        if not child:
            child = Note(
                title=title, 
                slug='untitled', 
                content='', 
                properties={}, 
                parent_id=journal.id, 
                is_template=False
            )
            db.session.add(child)
            db.session.commit()
            child.slug = f"{slugify(child.title) or 'note'}-{child.id}"
            db.session.commit()
            invalidate_parent_notes_cache()

        return jsonify({
            'ok': True, 
            'id': child.id, 
            'title': child.title, 
            'content': child.content or '',
            'properties': child.properties or {},
            'updated_at': child.updated_at.isoformat() + 'Z' if child.updated_at else ''
        })     
        
    @app.route('/note/<int:note_id>/duplicate', methods=['POST'])
    def note_duplicate(note_id):
        src = Note.query.get_or_404(note_id)
        
        copy = Note(
            title=f"{src.title} (copy)",
            slug='tmp',
            content=src.content or '',
            properties=dict(src.properties or {}),
            parent_id=src.parent_id,
            is_template=False,
            is_map_note=bool(src.is_map_note),
            due_at=src.due_at,
            notified=src.notified,
        )
        db.session.add(copy)
        db.session.flush()

        for att in src.attachments:
            ext = os.path.splitext(att.filename)[1]
            new_name = f"{uuid.uuid4().hex}{ext}"
            old_path = os.path.join(app.config['UPLOAD_FOLDER'], att.filename)
            new_path = os.path.join(app.config['UPLOAD_FOLDER'], new_name)
            try:
                shutil.copyfile(old_path, new_path)
            except OSError:
                continue
            new_att = Attachment(
                note_id=copy.id,
                filename=new_name,
                original_name=att.original_name,
                mimetype=att.mimetype,
            )
            db.session.add(new_att)

        copy.slug = f"{slugify(copy.title)}-{copy.id}"
        db.session.commit()
        return jsonify({'ok': True, 'id': copy.id})
        
    # ─── Chunked upload endpoints ────────────────────────────────────────
    # Used by the client for large files (PDFs, GPX, etc.) where a single
    # HTTP POST would time out on mobile. Each chunk is a separate small
    # POST; when all chunks are in, /upload/complete assembles them.

    @app.route('/upload/chunk', methods=['POST'])
    def upload_chunk():
        import uuid
        upload_id = request.form.get('upload_id', '').strip()
        chunk_index = request.form.get('chunk_index', type=int)
        total_chunks = request.form.get('total_chunks', type=int)
        filename = request.form.get('filename', '').strip()
        mimetype = request.form.get('mimetype', '').strip()

        if not upload_id or chunk_index is None or total_chunks is None or not filename:
            return jsonify({'ok': False, 'error': 'missing fields'}), 400

        # Validate upload_id is a hex uuid to prevent path traversal
        try:
            uuid.UUID(upload_id)
        except ValueError:
            return jsonify({'ok': False, 'error': 'invalid upload_id'}), 400

        chunk_file = request.files.get('chunk')
        if not chunk_file:
            return jsonify({'ok': False, 'error': 'missing chunk'}), 400

        chunk_dir = os.path.join(app.instance_path, 'chunk_uploads', upload_id)
        os.makedirs(chunk_dir, exist_ok=True)

        # Save this chunk
        chunk_path = os.path.join(chunk_dir, f'{chunk_index:06d}.part')
        chunk_file.save(chunk_path)

        return jsonify({
            'ok': True,
            'upload_id': upload_id,
            'chunk_index': chunk_index,
            'received': os.path.getsize(chunk_path),
        })

    @app.route('/upload/complete', methods=['POST'])
    def upload_complete():
        import shutil
        data = request.get_json() or {}
        upload_id = (data.get('upload_id') or '').strip()
        filename = (data.get('filename') or '').strip()
        mimetype = (data.get('mimetype') or '').strip()
        total_chunks = data.get('total_chunks')
        note_id = data.get('note_id')

        if not upload_id or not filename or not note_id or total_chunks is None:
            return jsonify({'ok': False, 'error': 'missing fields'}), 400

        try:
            uuid.UUID(upload_id)
        except ValueError:
            return jsonify({'ok': False, 'error': 'invalid upload_id'}), 400

        note = Note.query.get(int(note_id))
        if not note:
            return jsonify({'ok': False, 'error': 'note not found'}), 404

        chunk_dir = os.path.join(app.instance_path, 'chunk_uploads', upload_id)
        if not os.path.isdir(chunk_dir):
            return jsonify({'ok': False, 'error': 'no chunks found'}), 404

        # Verify all chunks are present
        for i in range(total_chunks):
            p = os.path.join(chunk_dir, f'{i:06d}.part')
            if not os.path.exists(p):
                return jsonify({'ok': False, 'error': f'missing chunk {i}'}), 400

        # Assemble into the upload folder
        orig = secure_filename(filename)
        ext = os.path.splitext(orig)[1].lower()
        base_name = os.path.splitext(orig)[0]
        stored = orig
        counter = 1
        while os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], stored)):
            stored = f"{base_name}_{counter}{ext}"
            counter += 1

        final_path = os.path.join(app.config['UPLOAD_FOLDER'], stored)
        with open(final_path, 'wb') as out:
            for i in range(total_chunks):
                p = os.path.join(chunk_dir, f'{i:06d}.part')
                with open(p, 'rb') as f:
                    shutil.copyfileobj(f, out)

        # Optional resize (image only, per existing setting)
        resize_image_if_needed(mimetype, final_path)

        # Create the Attachment row
        att = Attachment(
            note_id=note.id,
            filename=stored,
            original_name=orig,
            mimetype=mimetype,
        )
        db.session.add(att)
        db.session.commit()

        # Clean up chunk dir
        try:
            shutil.rmtree(chunk_dir)
        except OSError:
            pass

        return jsonify({
            'ok': True,
            'id': att.id,
            'url': url_for('uploaded_file', filename=stored),
            'name': orig,
            'is_image': att.is_image,
        })        

    @app.route('/note/<int:note_id>/upload', methods=['POST'])
    def upload(note_id):
        note = Note.query.get_or_404(note_id)
        files = request.files.getlist('file')
        created = []
        for f in files:
            if not f.filename:
                continue
            orig = secure_filename(f.filename)
            ext = os.path.splitext(orig)[1].lower()
            
            # Use the original filename, but add a counter if it exists
            base_name = os.path.splitext(orig)[0]
            stored = orig
            counter = 1
            while os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], stored)):
                stored = f"{base_name}_{counter}{ext}"
                counter += 1
                
            path = os.path.join(app.config['UPLOAD_FOLDER'], stored)
            f.save(path)
            resize_image_if_needed(f.mimetype, path)
            att = Attachment(note_id=note.id, filename=stored,
                             original_name=orig, mimetype=f.mimetype)
            db.session.add(att)
            db.session.flush()
            created.append({
                'id': att.id,
                'url': url_for('uploaded_file', filename=stored),
                'name': orig,
                'is_image': att.is_image,
            })
        db.session.commit()
        return jsonify({'files': created})

    @app.route('/uploads/<path:filename>')
    def uploaded_file(filename):
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename,
                                   as_attachment=False)
                                   
    @app.route('/tiles/<int:z>/<int:x>/<int:y>.png')
    def map_tile(z, x, y):
        """Proxy OSM tiles with a local disk cache.

        - First request for a tile: fetch from OSM, save to disk, serve.
        - Subsequent requests: serve from disk.
        - Works offline for any area already viewed at least once.

        Cache location: instance/tile_cache/{z}/{x}/{y}.png
        """
        import requests as req_lib

        cache_dir = os.path.join(app.instance_path, 'tile_cache', str(z), str(x))
        cache_path = os.path.join(cache_dir, f'{y}.png')

        if os.path.exists(cache_path):
            return send_from_directory(cache_dir, f'{y}.png', mimetype='image/png')

        # Not cached: fetch from OSM.
        url = f'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
        headers = {
            'User-Agent': 'notes-app-personal/1.0 (self-hosted)',
            'Referer': 'http://localhost:5000/',
        }
        try:
            r = req_lib.get(url, headers=headers, timeout=15)
        except Exception as e:
            print(f"[map_tile] fetch failed z={z} x={x} y={y}: {e}")
            return ('', 503)

        if r.status_code != 200 or not r.content:
            print(f"[map_tile] OSM returned {r.status_code} for z={z} x={x} y={y}")
            return ('', 404)

        # Save to cache (best-effort; concurrent writes are fine for personal use).
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_path, 'wb') as f:
                f.write(r.content)
        except Exception as e:
            print(f"[map_tile] cache write failed z={z} x={x} y={y}: {e}")

        return app.response_class(r.content, mimetype='image/png')
                                   
    @app.route('/api/notes')
    def api_notes():
        q = request.args.get('q', '').strip()
        limit = request.args.get('limit', 20, type=int)
        exclude = request.args.get('exclude', type=int)
        only_templates = request.args.get('is_template') == '1'

        # Fast path: use the in-memory quick-switcher index instead of
        # a full DB scan on every keystroke. The index is a list of
        # {id, title, title_norm, updated_at_ts} dicts cached in
        # current_app.config['_qs_index'] — invalidated on note save/delete.
        index = get_quick_switcher_index()

        if only_templates:
            # Templates are few — a direct DB query is fine.
            template_ids = {
                row[0] for row in
                db.session.query(Note.id).filter(
                    Note.is_template == True,  # noqa: E712
                    Note.deleted_at.is_(None),
                ).all()
            }
            pool = [e for e in index if e['id'] in template_ids]
        else:
            pool = index

        if exclude:
            pool = [e for e in pool if e['id'] != exclude]

        if q:
            scored = []
            for e in pool:
                s = fuzzy_score(q, e['title'])
                if s > 0:
                    scored.append((s, e['updated_at_ts'], e))
            # Higher score first; tiebreaker = newer first.
            scored.sort(key=lambda x: (-x[0], -x[1]))
            chosen = [e for _, _, e in scored[:limit]]
        else:
            # No query: most recently modified first.
            chosen = sorted(pool, key=lambda e: e['updated_at_ts'], reverse=True)[:limit]

        return jsonify([{'id': e['id'], 'title': e['title'], 'slug': ''} for e in chosen])
          
    @app.route('/api/prop-values')
    def api_prop_values():
        """Return the current property index as {key: [values...]}.

        Used by the client to refresh the advanced-search dropdowns mid-session
        after a save that changed a note's properties. Reads straight from the
        DB (ignores the in-memory cache) so it's always accurate.
        """
        from sqlalchemy import text
        prop_values = {}
        try:
            rows = db.session.execute(text("""
                SELECT LOWER(TRIM(j.key)) AS k, j.value AS v
                FROM note, json_each(note.properties) AS j
                WHERE j.key IS NOT NULL AND j.key != ''
                  AND j.value IS NOT NULL
            """)).all()
            for k, v in rows:
                if not k:
                    continue
                for piece in str(v).split(','):
                    piece = piece.strip().lower()
                    if piece:
                        prop_values.setdefault(k, set()).add(piece)
        except Exception as e:
            print(f"[api_prop_values] SQLite JSON failed ({e}), falling back to Python")
            for n in Note.query.all():
                for k, v in (n.properties or {}).items():
                    if not k:
                        continue
                    key = str(k).strip().lower()
                    for piece in str(v).split(','):
                        piece = piece.strip().lower()
                        if piece:
                            prop_values.setdefault(key, set()).add(piece)

        return jsonify({
            k: sorted(vs) for k, vs in prop_values.items()
        })
        

    @app.route('/api/uploads')
    def api_uploads():
        files = os.listdir(app.config['UPLOAD_FOLDER'])
        return jsonify([
            url_for('uploaded_file', filename=f)
            for f in files
            if not f.startswith('.')
        ])

    @app.route('/attachment/<int:att_id>/delete', methods=['POST'])
    def attachment_delete(att_id):
        att = Attachment.query.get_or_404(att_id)
        note = att.note

        try:
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], att.filename))
        except OSError:
            pass

        if note and note.content:
            lines = note.content.split('\n')
            new_lines = [line for line in lines if att.filename not in line]
            note.content = '\n'.join(new_lines)

        db.session.delete(att)
        db.session.commit()

        return jsonify({'ok': True})
        
    @app.route('/attachment/<int:att_id>/rename', methods=['POST'])
    def attachment_rename(att_id):
        att = Attachment.query.get_or_404(att_id)
        data = request.get_json() or {}
        new_name = (data.get('name') or '').strip()
        if not new_name:
            return jsonify({'ok': False, 'error': 'empty name'}), 400

        # 1. Sanitize and preserve extension
        old_name = att.original_name
        old_ext = os.path.splitext(old_name or '')[1].lower()
        new_ext = os.path.splitext(new_name)[1].lower()
        if old_ext and new_ext != old_ext:
            new_name = new_name + old_ext
            
        safe_new_name = secure_filename(new_name)
        base_name = os.path.splitext(safe_new_name)[0]
        ext = os.path.splitext(safe_new_name)[1]
        
        # 2. Physically rename the file on disk
        old_stored = att.filename
        new_stored = safe_new_name
        counter = 1
        
        upload_dir = app.config['UPLOAD_FOLDER']
        old_path = os.path.join(upload_dir, old_stored)
        new_path = os.path.join(upload_dir, new_stored)
        
        # Ensure we don't overwrite an existing file with the same name
        while os.path.exists(new_path) and new_stored != old_stored:
            new_stored = f"{base_name}_{counter}{ext}"
            new_path = os.path.join(upload_dir, new_stored)
            counter += 1
            
        if os.path.exists(old_path) and old_stored != new_stored:
            try:
                os.rename(old_path, new_path)
                att.filename = new_stored
            except OSError as e:
                print(f"[attachment_rename] Failed to rename file: {e}")
                # We could return an error, but it's better to just leave the filename as is
                # and update the display name only.

        # 3. Update DB fields
        att.original_name = new_name

        # 4. Rewrite the markdown reference in the note body
        # (Using the NEW physical filename in the URL)
        if att.note and att.note.content:
            content = att.note.content
            if old_name:
                # Replace alt/link text
                content = content.replace(f'![{old_name}]', f'![{new_name}]')
                content = content.replace(f'[{old_name}]', f'[{new_name}]')
            if old_stored != new_stored:
                # Replace the physical URL if it was changed
                content = content.replace(f'/uploads/{old_stored}', f'/uploads/{new_stored}')
            att.note.content = content

        db.session.commit()

        return jsonify({
            'ok': True,
            'id': att.id,
            'name': att.original_name,
            'filename': att.filename,
            'content': att.note.content if att.note else '',
        })      

    @app.route('/api/notes/by-title/<path:title>')
    def api_note_by_title(title):
        block_anchor = None
        if '#' in title:
            title, block_anchor = title.split('#', 1)
            block_anchor = block_anchor.strip()
        
        title = title.strip()
        clean_title = strip_accents(title)
        
        n = Note.query.filter(
            db.func.STRIP_ACCENTS(Note.title) == clean_title, 
            Note.deleted_at.is_(None)
        ).first()
        
        if not n:
            return ('', 404)
            
        d = n.to_dict()
        d['content'] = n.content or ''
        d['properties'] = n.properties or {}
        d['updated_at'] = n.updated_at.isoformat() + 'Z' if n.updated_at else ''
        
        if block_anchor:
            blocks = block_utils.split_blocks(n.content or '')
            target_id = None
            
            # 1. Exact block ID match (8 hex chars)
            if re.match(r'^[0-9a-f]{8}$', block_anchor):
                for b in blocks:
                    if b['id'] == block_anchor:
                        target_id = b['id']
                        break
            # 2. Text match fallback
            else:
                clean_anchor = strip_accents(block_anchor)
                target_id = None
                # Pass 1: exact containment
                for b in blocks:
                    if clean_anchor in strip_accents(b['text']):
                        target_id = b['id']
                        break
                # Pass 2: fuzzy — first 30 chars match, or the anchor
                # is a prefix/suffix of the block text
                if not target_id:
                    anchor_prefix = clean_anchor[:30]
                    for b in blocks:
                        btext = strip_accents(b['text'])
                        if anchor_prefix and anchor_prefix in btext:
                            target_id = b['id']
                            break
                # Pass 3: last resort — token overlap (count matching words)
                if not target_id:
                    anchor_words = set(clean_anchor.split())
                    best_overlap = 0
                    best_block = None
                    for b in blocks:
                        btext = strip_accents(b['text'])
                        bwords = set(btext.split())
                        overlap = len(anchor_words & bwords)
                        if overlap > best_overlap and overlap >= 3:
                            best_overlap = overlap
                            best_block = b
                    if best_block:
                        target_id = best_block['id']
                        
            if target_id:
                d['block_id'] = target_id
                
        return jsonify(d)
        
    @app.route('/api/note/<int:note_id>/blocks')
    def api_note_blocks(note_id):
        """Return the note's top-level blocks + any attached metadata.

        Used by the preview renderer (block cards) and by the block
        context menu.
        """
        n = Note.query.get(note_id)
        if not n or n.deleted_at is not None:
            return jsonify({'ok': False, 'error': 'not found'}), 404

        content = n.content or ''
        parsed = block_utils.split_blocks(content)

        # Load metadata for this note in one query
        metas = {m.block_id: m for m in
                 BlockMeta.query.filter_by(note_id=note_id).all()}

        out = []
        for b in parsed:
            kind = 'text'
            t = b['text'].lstrip()
            if t.startswith(('- [ ]', '- [x]', '- [X]', '* [ ]', '* [x]', '+ [ ]', '+ [x]')):
                kind = 'todo'
            elif t.startswith('```'):
                kind = 'code'
            elif t.startswith('!'):
                kind = 'image_or_embed'
            elif re.match(r'^#{1,6}\s', t):
                kind = 'heading'
            elif t.startswith('>'):
                kind = 'quote'
            elif re.match(r'^\s*([-*+]|\d+\.)\s', t):
                kind = 'list'

            m = metas.get(b['id'])
            item = {
                'id': b['id'],
                'text': b['text'],
                'kind': kind,
                'hash': b['hash'],
                'has_meta': m is not None,
                'due_at': (m.due_at.isoformat() + 'Z') if (m and m.due_at) else None,
                'reminder_sent': bool(m.reminder_sent) if m else False,
            }
            out.append(item)

        return jsonify({'ok': True, 'blocks': out})        

    @app.route('/api/block/move', methods=['POST'])
    def api_block_move():
        """Move a block from one note to another.

        Body:
          {
            source_note_id: int,
            block_id: str (optional),
            block_text: str,
            target_note_id: int,
            create_target: { title } (optional) — if provided,
              create a new note with this title and use its ID
              instead of target_note_id.
          }

        Returns: { ok, target_note_id, target_note_title }
        """
        data = request.get_json() or {}
        try:
            src_id = int(data.get('source_note_id'))
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid source_note_id'}), 400

        block_text = (data.get('block_text') or '').strip()
        block_id = data.get('block_id') or None

        src = Note.query.get(src_id)
        if not src or src.deleted_at is not None:
            return jsonify({'ok': False, 'error': 'source note not found'}), 404

        # ── Resolve target ────────────────────────────────────────
        tgt = None
        create_target = data.get('create_target')
        if isinstance(create_target, dict):
            new_title = (create_target.get('title') or '').strip()
            if not new_title:
                return jsonify({'ok': False, 'error': 'empty title'}), 400
            # Create a new note under the source's parent (or root)
            root = get_root_note()
            parent_id = src.parent_id or (root.id if root else None)
            tgt = Note(title=new_title, slug='untitled',
                       content='', properties={}, parent_id=parent_id,
                       is_template=False)
            db.session.add(tgt)
            db.session.flush()
            tgt.slug = f"{slugify(new_title) or 'note'}-{tgt.id}"
            db.session.commit()
        else:
            try:
                tgt_id = int(data.get('target_note_id'))
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': 'invalid target_note_id'}), 400
            tgt = Note.query.get(tgt_id)
            if not tgt or tgt.deleted_at is not None:
                return jsonify({'ok': False, 'error': 'target note not found'}), 404

        if tgt.id == src.id:
            return jsonify({'ok': False, 'error': 'target is the same as source'}), 400

        # ── Locate the block in the source ────────────────────────
        src_blocks = block_utils.split_blocks(src.content or '')
        target_block = None
        for b in src_blocks:
            if block_id and b['id'] == block_id:
                target_block = b
                break
            if block_text and b['text'] == block_text:
                target_block = b
                break
        if target_block is None:
            return jsonify({'ok': False, 'error': 'block not found in source'}), 404

        # ── Splice block out of the source ────────────────────────
        before = (src.content or '')[: target_block['start']]
        after = (src.content or '')[target_block['end'] :]
        new_src = before + after
        new_src = re.sub(r'\n{3,}', '\n\n', new_src).strip('\n')
        src.content = new_src

        # ── Append block text + (optional) marker to target ───────
        block_to_append = target_block['text']
        has_marker = bool(target_block['marker'])
        if has_marker:
            block_to_append = block_to_append + '\n' + target_block['marker']

        tgt_content = (tgt.content or '').rstrip('\n')
        if tgt_content:
            tgt_content += '\n\n' + block_to_append
        else:
            tgt_content = block_to_append
        tgt.content = tgt_content

        # ── Transfer block_meta row if any ────────────────────────
        if has_marker:
            meta_row = BlockMeta.query.filter_by(
                note_id=src.id, block_id=target_block['id']
            ).first()
            if meta_row:
                # Delete from source, create on target with same ID/due
                db.session.delete(meta_row)
                db.session.flush()
                new_meta = BlockMeta(
                    note_id=tgt.id,
                    block_id=target_block['id'],
                    due_at=meta_row.due_at,
                    reminder_sent=meta_row.reminder_sent,
                    metadata_json=meta_row.metadata_json,
                )
                db.session.add(new_meta)

        db.session.commit()
        invalidate_prop_cache()
        invalidate_parent_notes_cache()

        return jsonify({
            'ok': True,
            'target_note_id': tgt.id,
            'target_note_title': tgt.title,
        })    
        
    @app.route('/api/note/<int:note_id>/blocks/search')
    def api_note_blocks_search(note_id):
        q = request.args.get('q', '').strip()
        note = Note.query.get(note_id)
        if not note:
            return jsonify([])
            
        clean_q = strip_accents(q)
        blocks = block_utils.split_blocks(note.content or '')
        out = []
        for b in blocks:
            if not b['text'].strip():
                continue
            if clean_q in strip_accents(b['text']):
                preview = b['text'].replace('\n', ' ')
                if len(preview) > 50:
                    preview = preview[:50] + '...'
                out.append({
                    'id': b['id'],
                    'text': preview
                })
        return jsonify(out)        
        
    @app.route('/api/block/update', methods=['POST'])
    def api_block_update():
        data = request.get_json() or {}
        try:
            note_id = int(data.get('note_id'))
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid note_id'}), 400
            
        block_id = data.get('block_id')
        new_text = (data.get('text') or '').strip()
        
        note = Note.query.get(note_id)
        if not note or note.deleted_at is not None:
            return jsonify({'ok': False, 'error': 'note not found'}), 404

        blocks = block_utils.split_blocks(note.content or '')
        target = None
        for b in blocks:
            # Match by ID (if the block already had a marker)
            if block_id and b['id'] == block_id:
                target = b
                break
                
        if not target:
            # Fallback: If we couldn't find it by ID, try to find it by text
            for b in blocks:
                if b['text'] == data.get('old_text', ''):
                    target = b
                    break

        if not target:
            return jsonify({'ok': False, 'error': 'block not found'}), 404

        # If the block didn't have a marker, generate one now so it's stable for the future
        if not target['marker']:
            target['marker'] = f"<!--blk:{block_utils.new_block_id()}-->"

        new_block_raw = new_text
        if target['marker']:
            new_block_raw += '\n' + target['marker']

        before = (note.content or '')[:target['start']]
        after = (note.content or '')[target['end']:]
        note.content = before + new_block_raw + after
        
        db.session.commit()
        invalidate_prop_cache()
        invalidate_parent_notes_cache()

        import time
        Setting.set(f'last_change_{note.id}', str(int(time.time() * 1000)))
        Setting.set(f'last_change_client_{note.id}', 'external')

        return jsonify({'ok': True, 'content': note.content})
        
    @app.route('/note/<int:note_id>/block/<block_id>/mark-done', methods=['GET', 'POST'])
    def block_mark_done(note_id, block_id):
        note = Note.query.get_or_404(note_id)
        row = BlockMeta.query.filter_by(note_id=note_id, block_id=block_id).first()
        if not row:
            return jsonify({'ok': False, 'error': 'block not found'}), 404
        row.due_at = None
        row.reminder_sent = True
        db.session.commit()
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        import time
        Setting.set(f'last_change_{note_id}', str(int(time.time() * 1000)))
        Setting.set(f'last_change_client_{note_id}', 'external')
        return jsonify({'ok': True}), 200

    @app.route('/note/<int:note_id>/block/<block_id>/snooze', methods=['GET', 'POST'])
    def block_snooze(note_id, block_id):
        from datetime import timedelta
        from sqlalchemy.orm.attributes import flag_modified
        import time
        note = Note.query.get_or_404(note_id)
        row = BlockMeta.query.filter_by(note_id=note_id, block_id=block_id).first()
        if not row:
            return jsonify({'ok': False, 'error': 'block not found'}), 404
        base = row.due_at or datetime.utcnow()
        row.due_at = base + timedelta(minutes=30)
        row.reminder_sent = False
        db.session.commit()
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        Setting.set(f'last_change_{note_id}', str(int(time.time() * 1000)))
        Setting.set(f'last_change_client_{note_id}', 'external')
        return jsonify({
            'ok': True,
            'new_due_at': row.due_at.isoformat() + 'Z'
        }), 200

    @app.route('/note/<int:note_id>/block/<block_id>/cancel-reminder', methods=['GET', 'POST'])
    def block_cancel_reminder(note_id, block_id):
        note = Note.query.get_or_404(note_id)
        row = BlockMeta.query.filter_by(note_id=note_id, block_id=block_id).first()
        if not row:
            return jsonify({'ok': False, 'error': 'block not found'}), 404
        row.due_at = None
        row.reminder_sent = True
        db.session.commit()
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        import time
        Setting.set(f'last_change_{note_id}', str(int(time.time() * 1000)))
        Setting.set(f'last_change_client_{note_id}', 'external')
        return jsonify({'ok': True}), 200        
        
    @app.route('/api/notes/<int:note_id>')
    def api_note_by_id(note_id):
        n = Note.query.get(note_id)
        if not n or n.deleted_at is not None:
            return jsonify({'ok': False, 'error': 'not found'}), 404

        has_children = bool(
            Note.query
            .filter(Note.parent_id == n.id, Note.deleted_at.is_(None))
            .with_entities(Note.id)
            .first()
        )

        child_count = (
            Note.query
            .filter(Note.parent_id == n.id, Note.deleted_at.is_(None))
            .count()
        )

        return jsonify({
            'ok': True,
            'id': n.id,
            'title': n.title,
            'content': n.content or '',
            'properties': n.properties or {},
            'updated_at': n.updated_at.isoformat() + 'Z' if n.updated_at else '',
            'due_at': n.due_at.isoformat() + 'Z' if n.due_at else None,
            'parent_id': n.parent_id,
            'parent_title': n.parent.title if n.parent else None,
            'is_map_note': bool(n.is_map_note),
            'is_template': bool(n.is_template),
            'blank_title_on_use': bool(n.blank_title_on_use),
            'has_children': has_children,
            'child_count': child_count,
            'link_ids': [o.id for o in (n.outbound or [])],
        })

    @app.route('/note/create-by-title', methods=['POST'])
    def note_create_by_title():
        data = request.get_json() or {}
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'ok': False, 'error': 'empty title'}), 400

        existing = Note.query.filter(Note.title.ilike(title)).first()
        if existing:
            return jsonify({'ok': True, 'id': existing.id, 'title': existing.title})

        root = get_root_note()
        parent_id = root.id if root else None

        note = Note(
            title=title,
            slug='tmp',
            content='',
            properties={},
            parent_id=parent_id,
            is_template=False,
        )
        db.session.add(note)
        db.session.commit()
        note.slug = f"{slugify(title)}-{note.id}"
        db.session.commit()
        
        # Invalidate caches so the sidebar reflects the new note
        invalidate_prop_cache()
        invalidate_parent_notes_cache()
        
        return jsonify({'ok': True, 'id': note.id, 'title': note.title})

    @app.route('/note/create-child', methods=['POST'])
    def note_create_child():
        """Create a new child note under a given parent, with title and content."""
        data = request.get_json() or {}
        title = (data.get('title') or '').strip()
        content = data.get('content') or ''
        parent_id = data.get('parent_id')

        # Fallback for blank titles: use a timestamp-based name.
        if not title:
            title = datetime.now().strftime('Note %Y-%m-%d %H-%M')

        # Safely handle the parent ID (which might come in as a string,
        # an int, or be missing entirely).
        parent = None
        if parent_id is not None:
            try:
                parent = Note.query.get(int(parent_id))
            except (ValueError, TypeError):
                parent = None
                
        if not parent:
            parent = get_root_note()

        note = Note(
            title=title,
            slug='untitled',
            content=content,
            properties={},
            parent_id=parent.id,
            is_template=False,
        )
        db.session.add(note)
        db.session.commit()
        note.slug = f"{slugify(title)}-{note.id}"
        db.session.commit()

        invalidate_prop_cache()
        invalidate_parent_notes_cache()

        return jsonify({
            'ok': True,
            'id': note.id,
            'title': note.title,
            'url': url_for('index', note=note.id, parent=parent.id),
        })    
        
    # ---------- Captures ----------
    @app.route('/captures', methods=['GET'])
    def captures_list():
        rows = Capture.query.order_by(Capture.position.asc(), Capture.id.asc()).all()
        return jsonify([{
            'id': c.id,
            'name': c.name,
            'kind': c.kind,
            'target_id': c.target_id,
            'target_title': c.target.title if c.target else '',
            'title_template': c.title_template or '',
            'body_template': c.body_template or '',
            'template_id': c.template_id,
            'template_title': c.template.title if c.template else '',
            'after_action': c.after_action or 'toast',
            'text_prefix': c.text_prefix or '',
            'text_suffix': c.text_suffix or '',
        } for c in rows])

    @app.route('/captures', methods=['POST'])
    def captures_create():
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        kind = (data.get('kind') or '').strip()
        target_id = data.get('target_id')

        if not name:
            return jsonify({'ok': False, 'error': 'empty name'}), 400
        if kind not in ('new_child', 'append_text', 'attach_file', 'daily_note'):
            return jsonify({'ok': False, 'error': 'invalid kind'}), 400

        if kind == 'daily_note':
            j = _get_journal_parent()
            c = Capture(name=name, kind='daily_note', target_id=j.id)
            c.title_template = (data.get('title_template') or '{{date}}').strip()
            c.body_template = data.get('body_template') or ''
            tpl_id = data.get('template_id')
            if tpl_id:
                tpl = Note.query.get(int(tpl_id))
                if tpl:
                    c.template_id = tpl.id
            
            # Process combined "action:navigation" string
            after = (data.get('after_action') or 'append_text').strip()
            valid_actions = ('append_text', 'attach_file', 'open')
            
            if ':' in after:
                action_part, nav_part = after.split(':', 1)
                if action_part not in valid_actions:
                    action_part = 'append_text'
                if nav_part not in ('stay', 'navigate'):
                    nav_part = 'stay'
                after = f"{action_part}:{nav_part}"
            else:
                if after not in valid_actions:
                    after = 'append_text'
                after = f"{after}:stay"
                
            c.after_action = after
            c.text_prefix = (data.get('text_prefix') or '').strip()
            c.text_suffix = (data.get('text_suffix') or '').strip()
            db.session.add(c)
            db.session.commit()
            return jsonify({'ok': True, 'id': c.id})

        if not target_id:
            return jsonify({'ok': False, 'error': 'target required'}), 400

        target = Note.query.get(int(target_id))
        if not target:
            return jsonify({'ok': False, 'error': 'target not found'}), 400

        c = Capture(name=name, kind=kind, target_id=target.id)
        c.text_prefix = (data.get('text_prefix') or '').strip()
        c.text_suffix = (data.get('text_suffix') or '').strip()
        if kind == 'new_child':
            c.title_template = (data.get('title_template') or '').strip()
            c.body_template = data.get('body_template') or ''

            tpl_obj = None
            tpl_id = data.get('template_id')
            if tpl_id:
                tpl_obj = Note.query.get(int(tpl_id))
                if tpl_obj:
                    c.template_id = tpl_obj.id

            after = (data.get('after_action') or 'navigate').strip()
            if after not in ('navigate', 'stay'):
                after = 'navigate'
            if after == 'stay' and not c.title_template and not c.template_id:
                return jsonify({
                    'ok': False,
                    'error': 'Stay put requires a title template or template note.'
                }), 400
            if (after == 'stay'
                    and not c.title_template
                    and tpl_obj is not None
                    and tpl_obj.blank_title_on_use):
                return jsonify({
                    'ok': False,
                    'error': 'Stay put requires a title: the selected template '
                             'has "Blank title on use" enabled. Add a title '
                             'template, or switch to "Open the new note".'
                }), 400
            c.after_action = after
        else:
            after = (data.get('after_action') or 'toast').strip()
            if after not in ('navigate', 'toast', 'stay'):
                after = 'toast'
            c.after_action = after

        db.session.add(c)
        db.session.commit()
        return jsonify({'ok': True, 'id': c.id})

    @app.route('/captures/<int:cap_id>/delete', methods=['POST'])
    def captures_delete(cap_id):
        c = Capture.query.get_or_404(cap_id)
        db.session.delete(c)
        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/captures/<int:cap_id>/update', methods=['POST'])
    def captures_update(cap_id):
        c = Capture.query.get_or_404(cap_id)
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        kind = (data.get('kind') or '').strip()
        target_id = data.get('target_id')

        if not name:
            return jsonify({'ok': False, 'error': 'empty name'}), 400
        if kind not in ('new_child', 'append_text', 'attach_file', 'daily_note'):
            return jsonify({'ok': False, 'error': 'invalid kind'}), 400

        if kind == 'daily_note':
            j = _get_journal_parent()
            c.name = name
            c.kind = 'daily_note'
            c.target_id = j.id
            c.title_template = (data.get('title_template') or '{{date}}').strip()
            c.body_template = data.get('body_template') or ''
            tpl_id = data.get('template_id')
            c.template_id = None
            if tpl_id:
                tpl = Note.query.get(int(tpl_id))
                if tpl:
                    c.template_id = tpl.id
            
            # Process combined "action:navigation" string
            after = (data.get('after_action') or 'append_text').strip()
            valid_actions = ('append_text', 'attach_file', 'open')
            
            if ':' in after:
                action_part, nav_part = after.split(':', 1)
                if action_part not in valid_actions:
                    action_part = 'append_text'
                if nav_part not in ('stay', 'navigate'):
                    nav_part = 'stay'
                after = f"{action_part}:{nav_part}"
            else:
                if after not in valid_actions:
                    after = 'append_text'
                after = f"{after}:stay" # Default navigation
                
            c.after_action = after
            c.text_prefix = (data.get('text_prefix') or '').strip()
            c.text_suffix = (data.get('text_suffix') or '').strip()
            db.session.commit()
            return jsonify({'ok': True, 'id': c.id})

        if not target_id:
            return jsonify({'ok': False, 'error': 'target required'}), 400

        target = Note.query.get(int(target_id))
        if not target:
            return jsonify({'ok': False, 'error': 'target not found'}), 400

        c.name = name
        c.kind = kind
        c.target_id = target.id
        c.text_prefix = (data.get('text_prefix') or '').strip()
        c.text_suffix = (data.get('text_suffix') or '').strip()

        if kind == 'new_child':
            c.title_template = (data.get('title_template') or '').strip()
            c.body_template = data.get('body_template') or ''

            tpl_obj = None
            tpl_id = data.get('template_id')
            c.template_id = None
            if tpl_id:
                tpl_obj = Note.query.get(int(tpl_id))
                if tpl_obj:
                    c.template_id = tpl_obj.id

            after = (data.get('after_action') or 'navigate').strip()
            if after not in ('navigate', 'stay'):
                after = 'navigate'
            if after == 'stay' and not c.title_template and not c.template_id:
                return jsonify({
                    'ok': False,
                    'error': 'Stay put requires a title template or template note.'
                }), 400
            if (after == 'stay'
                    and not c.title_template
                    and tpl_obj is not None
                    and tpl_obj.blank_title_on_use):
                return jsonify({
                    'ok': False,
                    'error': 'Stay put requires a title: the selected template '
                             'has "Blank title on use" enabled. Add a title '
                             'template, or switch to "Open the new note".'
                }), 400
            c.after_action = after
        else:
            after = (data.get('after_action') or 'toast').strip()
            if after not in ('navigate', 'toast', 'stay'):
                after = 'toast'
            c.after_action = after
            c.title_template = ''
            c.body_template = ''
            c.template_id = None

        db.session.commit()
        return jsonify({'ok': True, 'id': c.id})

    @app.route('/captures/<int:cap_id>/run', methods=['POST'])
    def captures_run(cap_id):
        c = Capture.query.get_or_404(cap_id)
        target = c.target
        if not target and c.kind != 'daily_note':
            return jsonify({'ok': False, 'error': 'target missing'}), 400

        if c.kind == 'daily_note':
            # Split the combined "action:nav" string
            parts = (c.after_action or 'append_text:stay').split(':')
            action = parts[0]
            after_nav = parts[1] if len(parts) > 1 else 'stay'

            journal = _get_journal_parent()
            title = substitute_vars(c.title_template or '{{date}}').strip() or substitute_vars('{{date}}')
            
            # Define the date variable for template substitution
            d = datetime.now()
            
            child = Note.query.filter(
                Note.parent_id == journal.id,
                Note.title == title,
                Note.deleted_at.is_(None)
            ).order_by(Note.id.asc()).first()

            if not child:
                new_content = ''
                new_props = {}
                
                tpl_id = Setting.get('daily_note_template_id', '')
                if tpl_id and tpl_id.isdigit():
                    tpl = Note.query.get(int(tpl_id))
                    if tpl:
                        # Copy content and substitute the date
                        new_content = tpl.content or ''
                        new_content = new_content.replace('{{date}}', d.strftime('%Y-%m-%d'))
                        
                        # Copy properties EXACTLY as they are in the template
                    new_props = {}
                    for k, v in (tpl.properties or {}).items():
                        # Convert None to empty string
                        val_str = '' if v is None else str(v)
                        val_str = val_str.replace('{{date}}', d.strftime('%Y-%m-%d'))
                        new_props[str(k).strip().lower()] = val_str

                child = Note(
                    title=title,
                    slug='untitled',
                    content=new_content,
                    properties=sort_props_status_last(new_props),
                    parent_id=journal.id,
                    is_template=False,
                    deleted_at=None,
                )
                db.session.add(child)
                db.session.commit()
                
                child.slug = f"{slugify(child.title) or 'note'}-{child.id}"
                db.session.commit()
                
                invalidate_prop_cache()
                invalidate_parent_notes_cache()

            if action == 'append_text':
                text = (request.get_json(silent=True) or {}).get('text', '').strip()
                if not text:
                    return jsonify({'ok': False, 'error': 'empty text'}), 400
                text = substitute_vars(text)
                text = _apply_text_template(
                    substitute_vars(c.text_prefix or '').replace('\\n', '\n'),
                    substitute_vars(c.text_suffix or '').replace('\\n', '\n'),
                    text,
                )
                current = child.content or ''
                if current and not current.endswith('\n\n'):
                    if current.endswith('\n'):
                        current += '\n'
                    else:
                        current += '\n\n'
                child.content = current + text
                db.session.commit()
                return jsonify({
                    'ok': True,
                    'kind': 'daily_note',
                    'action': 'append_text',
                    'id': child.id,
                    'title': child.title,
                    'url': url_for('index', note=child.id, nofocus='1'),
                    'after': after_nav,
                    'content': child.content or '',
                })

            if action == 'attach_file':
                f = request.files.get('file')
                if not f or not f.filename:
                    return jsonify({'ok': False, 'error': 'no file'}), 400
                orig = secure_filename(f.filename)
                ext = os.path.splitext(orig)[1].lower()
                base_name = os.path.splitext(orig)[0]
                stored = orig
                counter = 1
                while os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], stored)):
                    stored = f"{base_name}_{counter}{ext}"
                    counter += 1
                path = os.path.join(app.config['UPLOAD_FOLDER'], stored)
                f.save(path)
                resize_image_if_needed(f.mimetype, path)
                att = Attachment(note_id=child.id, filename=stored,
                                 original_name=orig, mimetype=f.mimetype)
                db.session.add(att)
                db.session.flush()

                file_url = url_for('uploaded_file', filename=stored)
                is_image = (f.mimetype or '').startswith('image/')
                snippet = f"![{orig}]({file_url})" if is_image else f"[{orig}]({file_url})"
                snippet = _apply_text_template(
                    substitute_vars(c.text_prefix or '').replace('\\n', '\n'),
                    substitute_vars(c.text_suffix or '').replace('\\n', '\n'),
                    snippet,
                )
                current = child.content or ''
                if current and not current.endswith('\n\n'):
                    if current.endswith('\n'):
                        current += '\n'
                    else:
                        current += '\n\n'
                child.content = current + snippet + '\n\n'
                db.session.commit()
                return jsonify({
                    'ok': True,
                    'kind': 'daily_note',
                    'action': 'attach_file',
                    'id': child.id,
                    'title': child.title,
                    'url': file_url,
                    'note_url': url_for('index', note=child.id, nofocus='1'),
                    'content': child.content or '',
                    'after': after_nav,
                })

            if action == 'open':
                return jsonify({
                    'ok': True,
                    'kind': 'daily_note',
                    'action': 'open',
                    'id': child.id,
                    'title': child.title,
                    'url': url_for('index', note=child.id, nofocus='1'),
                    'after': after_nav,
                })

            return jsonify({'ok': False, 'error': 'invalid action'}), 400

        if c.kind == 'new_child':
            template_wants_blank = bool(c.template and c.template.blank_title_on_use)
            capture_has_title_tpl = bool(c.title_template)

            if template_wants_blank:
                new_title = ''
            elif capture_has_title_tpl:
                new_title = substitute_vars(c.title_template).strip()
            elif c.template:
                new_title = substitute_vars(c.template.title or '')
            else:
                new_title = ''

            new_content = substitute_vars(c.body_template or '')
            new_props = {}

            if c.template:
                if not new_content:
                    new_content = substitute_vars(c.template.content or '')
                new_props = {
                    k: substitute_vars(str(v))
                    for k, v in (c.template.properties or {}).items()
                }

            if not new_title and not template_wants_blank and not (not capture_has_title_tpl and not c.template):
                new_title = 'Untitled'

            note = Note(
                title=new_title,
                slug='untitled',
                content=new_content,
                properties=new_props,
                parent_id=target.id,
                is_template=False,
            )
            db.session.add(note)
            db.session.commit()
            note.slug = f"{slugify(note.title)}-{note.id}"
            db.session.commit()
            db.session.expire(note)

            import time
            t = int(time.time() * 1000)
            focus = ''
            if not c.title_template and not c.template:
                focus = 'title'
            elif c.template and c.template.blank_title_on_use:
                focus = 'title'
            url = url_for('index', note=note.id, parent=target.id, t=t)
            if focus:
                url += '&focus=' + focus
            return jsonify({
                'ok': True,
                'kind': 'new_child',
                'id': note.id,
                'url': url,
                'title': note.title,
                'after': c.after_action or 'navigate',
            })

        if c.kind == 'append_text':
            text = (request.get_json(silent=True) or {}).get('text', '').strip()
            if not text:
                return jsonify({'ok': False, 'error': 'empty text'}), 400
            text = substitute_vars(text)
            text = _apply_text_template(
                substitute_vars(c.text_prefix or '').replace('\\n', '\n'),
                substitute_vars(c.text_suffix or '').replace('\\n', '\n'),
                text,
            )
            current = target.content or ''
            if current and not current.endswith('\n'):
                current += '\n'
            target.content = current + text
            db.session.commit()
            return jsonify({'ok': True, 'kind': 'append_text', 'after': c.after_action})

        if c.kind == 'attach_file':
            f = request.files.get('file')
            if not f or not f.filename:
                return jsonify({'ok': False, 'error': 'no file'}), 400
            orig = secure_filename(f.filename)
            ext = os.path.splitext(orig)[1].lower()
            base_name = os.path.splitext(orig)[0]
            stored = orig
            counter = 1
            while os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], stored)):
                stored = f"{base_name}_{counter}{ext}"
                counter += 1
            path = os.path.join(app.config['UPLOAD_FOLDER'], stored)
            f.save(path)
            resize_image_if_needed(f.mimetype, path)
            att = Attachment(note_id=target.id, filename=stored,
                             original_name=orig, mimetype=f.mimetype)
            db.session.add(att)
            db.session.flush()

            file_url = url_for('uploaded_file', filename=stored)
            is_image = (f.mimetype or '').startswith('image/')
            snippet = f"![{orig}]({file_url})" if is_image else f"[{orig}]({file_url})"
            snippet = _apply_text_template(
                substitute_vars(c.text_prefix or '').replace('\\n', '\n'),
                substitute_vars(c.text_suffix or '').replace('\\n', '\n'),
                snippet,
            )
            current = target.content or ''
            if current and not current.endswith('\n'):
                current += '\n'
            target.content = current + snippet + '\n'
            db.session.commit()
            return jsonify({
                'ok': True,
                'kind': 'attach_file',
                'after': c.after_action,
                'url': file_url,
            })

        return jsonify({'ok': False, 'error': 'unknown kind'}), 400


def run_startup_migrations(app):
    with app.app_context():
        bootstrap_root(app)


app = create_app()
run_startup_migrations(app)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)