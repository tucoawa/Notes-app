import os
import re
import sys
import zipfile
import yaml
import urllib.parse
from datetime import datetime
from slugify import slugify
from app import create_app
from extensions import db
from models import Note, Setting

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+", flags=re.UNICODE
)

ATTACHMENT_FOLDERS = {'assets', 'attachments', 'files', 'media', '_resources'}


def remove_emojis(text):
    return EMOJI_PATTERN.sub(r'', text).strip()


def fix_encoding(text):
    try:
        return text.encode('cp437').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def parse_frontmatter(content):
    frontmatter = {}
    body = content
    if content.startswith('---\n') or content.startswith('---\r\n'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
                body = parts[2].lstrip('\r\n')
            except Exception:
                manual_props = {}
                for line in parts[1].split('\n'):
                    if ':' in line:
                        key, val = line.split(':', 1)
                        key = key.strip().lower()
                        val = val.strip().strip('"').strip("'")
                        if key and val:
                            manual_props[key] = val
                frontmatter = manual_props
                body = parts[2].lstrip('\r\n')
    return frontmatter, body


def clean_properties(frontmatter):
    props = {}
    done_val = frontmatter.pop('done', None)
    if done_val is True or str(done_val).lower() == 'true':
        props['status'] = 'done'
    elif done_val is False or str(done_val).lower() == 'false':
        props['status'] = ''

    for k, v in frontmatter.items():
        if not k or k == 'aliases' or v is None:
            continue
        key = str(k).strip().lower()
        if key == 'created':
            continue
        if isinstance(v, list):
            values_list = [str(x).strip().lower() for x in v]
        elif isinstance(v, datetime):
            props[key] = v.strftime('%Y-%m-%d')
            continue
        else:
            val_str = str(v).strip().lower()
            values_list = [x.strip() for x in val_str.split(',') if x.strip()]

        if key == 'category':
            values_list = [x for x in values_list if x != 'daily_logs']

        values_list = [x for x in values_list if '{{' not in x]
        if not values_list:
            continue
        props[key] = remove_emojis(', '.join(values_list))
    return props


def process_wikilinks(body, attachments_map):
    def replace_link(m):
        full = m.group(0)
        is_embed = full.startswith('!')
        inner_text = m.group(2).strip()
        url = attachments_map.get(inner_text) or attachments_map.get(inner_text.lower())
        if url:
            safe_url = urllib.parse.quote(url)
            if inner_text.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')):
                return f"![{inner_text}]({safe_url})" if is_embed else f"[{inner_text}]({safe_url})"
            else:
                return f"[{inner_text}]({safe_url})"
        return full
    pattern = re.compile(r'(!?)\[\[\s*(.*?)\s*\]\]', re.DOTALL)
    return pattern.sub(replace_link, body)


def get_or_create_folder(parent_id, folder_name, folder_map, path_key, root_id):
    """Create a folder note under `parent_id` if not already in folder_map."""
    if path_key in folder_map:
        return folder_map[path_key]
    note = Note(
        title=folder_name,
        slug=f"{slugify(folder_name)}-tmp",
        content='',
        properties={},
        parent_id=parent_id,
        is_template=False,
    )
    db.session.add(note)
    db.session.flush()
    note.slug = f"{slugify(folder_name)}-{note.id}"
    folder_map[path_key] = note.id
    return note.id


def process_obsidian_zip(zip_path):
    app = create_app()
    with app.app_context():
        db_path = app.config['DB_PATH']
        upload_dir = app.config['UPLOAD_FOLDER']
        os.makedirs(upload_dir, exist_ok=True)

        print(f"⚠️  IMPORTING INTO DATABASE: {db_path}")
        print(f"⚠️  UPLOADING TO: {upload_dir}")
        confirm = input("Are you sure you want to proceed? (y/n): ")
        if confirm.lower() != 'y':
            print("Aborted.")
            return

        print(f"Opening ZIP: {zip_path}")

        # --- Root note ---
        root = None
        stored_id = Setting.get('root_note_id', '').strip()
        if stored_id.isdigit():
            root = Note.query.get(int(stored_id))
        if not root:
            root = Note(title='Root', slug='root', content='', parent_id=None,
                        properties={}, is_template=False)
            db.session.add(root)
            db.session.commit()
            root.slug = f"root-{root.id}"
            db.session.commit()
            Setting.set('root_note_id', str(root.id))
            print(f"Created Root (ID: {root.id})")
        else:
            print(f"Using Root (ID: {root.id})")

        attachments_map = {}
        folder_map = {}       # path_string -> Note.id
        known_note_titles = set()
        known_note_titles.add(root.title.lower())

        with zipfile.ZipFile(zip_path, 'r') as zf:
            all_files = sorted(zf.namelist())

            # --- PASS 1: attachments ---
            for zip_path_item in all_files:
                if zip_path_item.endswith('/') or zip_path_item.endswith('.md'):
                    continue
                lower = zip_path_item.lower()
                if '.obsidian' in lower or '.trash' in lower:
                    continue
                base_name = fix_encoding(zip_path_item.split('/')[-1])
                if base_name.startswith('.'):
                    continue
                dest = os.path.join(upload_dir, base_name)
                with zf.open(zip_path_item) as src, open(dest, 'wb') as dst:
                    dst.write(src.read())
                url = f"/uploads/{base_name}"
                attachments_map[base_name] = url
                attachments_map[base_name.lower()] = url
            print(f"Extracted {len(attachments_map)} attachments.")

            # --- PASS 2: create folders ---
            # Journal handling: any folder whose path contains "Journal"
            # collapses to a single "Journal" note under Root.
            journal_id = get_or_create_folder(
                root.id, 'Journal', folder_map, '__JOURNAL__', root.id
            )
            known_note_titles.add('journal')

            for zip_path_item in all_files:
                if not zip_path_item.endswith('/'):
                    continue
                clean_path = zip_path_item.rstrip('/')
                if not clean_path:
                    continue
                parts = clean_path.split('/')

                # Skip attachment folders
                if parts[-1].lower() in ATTACHMENT_FOLDERS:
                    continue

                # Journal subtree: register all paths to Journal
                if 'Journal' in parts or 'journal' in [p.lower() for p in parts]:
                    folder_map[clean_path] = journal_id
                    continue

                # Normal folder: create under its parent
                folder_name = remove_emojis(fix_encoding(parts[-1]))
                parent_path = '/'.join(parts[:-1])
                parent_id = root.id
                if parent_path and parent_path in folder_map:
                    parent_id = folder_map[parent_path]
                new_id = get_or_create_folder(
                    parent_id, folder_name, folder_map, clean_path, root.id
                )
                known_note_titles.add(folder_name.lower())

            db.session.commit()
            print(f"Created {len(folder_map)} folders.")

            # --- PASS 3: notes ---
            orphan_count = 0
            for zip_path_item in all_files:
                if not zip_path_item.endswith('.md'):
                    continue
                parts = zip_path_item.split('/')
                filename = remove_emojis(fix_encoding(parts[-1][:-3]))
                parent_path = '/'.join(parts[:-1])

                # Journal notes → attach to Journal
                if any(p.lower() == 'journal' for p in parts):
                    parent_id = journal_id
                elif parent_path and parent_path in folder_map:
                    parent_id = folder_map[parent_path]
                else:
                    parent_id = root.id
                    orphan_count += 1
                    print(f"  [ROOT] {zip_path_item} → attached to Root")

                raw_content = zf.read(zip_path_item).decode('utf-8')
                frontmatter, body = parse_frontmatter(raw_content)
                if body.strip().startswith(f'# {filename}'):
                    body = body.strip()[len(f'# {filename}'):].strip()
                props = clean_properties(frontmatter)
                body = remove_emojis(body)
                body = process_wikilinks(body, attachments_map)

                note = Note(
                    title=filename,
                    slug=f"{slugify(filename)}-tmp",
                    content=body,
                    properties=props,
                    parent_id=parent_id,
                    is_template=False,
                )
                db.session.add(note)
                db.session.flush()
                note.slug = f"{slugify(filename)}-{note.id}"
                known_note_titles.add(filename.lower())

            db.session.commit()
            print(f"\nSuccessfully imported into {db_path}!")
            print(f"Vault-root notes attached directly to Root: {orphan_count}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python import_obsidian.py path/to/vault.zip")
        sys.exit(1)
    zip_path = sys.argv[1]
    if not os.path.exists(zip_path):
        print(f"Error: File not found: {zip_path}")
        sys.exit(1)
    process_obsidian_zip(zip_path)