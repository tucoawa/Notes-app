from datetime import datetime
from slugify import slugify
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from extensions import db


note_links = db.Table(
    'note_links',
    db.Column('src_id', db.Integer, db.ForeignKey('note.id'), primary_key=True),
    db.Column('dst_id', db.Integer, db.ForeignKey('note.id'), primary_key=True),
)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Setting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, default='')

    @staticmethod
    def get(key, default=''):
        row = Setting.query.get(key)
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = Setting.query.get(key)
        if row:
            row.value = value
        else:
            row = Setting(key=key, value=value)
            db.session.add(row)
        db.session.commit()


class Note(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False, default='Untitled', index=True)
    slug = db.Column(db.String(220), index=True)
    content = db.Column(db.Text, default='')
    properties = db.Column(db.JSON, default=dict)

    parent_id = db.Column(db.Integer, db.ForeignKey('note.id'), index=True)
    is_folder = db.Column(db.Boolean, default=False)
    is_template = db.Column(db.Boolean, default=False)
    blank_title_on_use = db.Column(db.Boolean, default=False)
    is_map_note = db.Column(db.Boolean, default=False, index=True)

    sort_by = db.Column(db.String(20), default='modified')
    sort_dir = db.Column(db.String(4), default='asc')

    due_at = db.Column(db.DateTime)
    notified = db.Column(db.Boolean, default=False)
    notify_attempts = db.Column(db.Integer, default=0)
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    children = db.relationship(
        'Note',
        backref=db.backref('parent', remote_side=[id]),
        cascade='save-update',
        foreign_keys=[parent_id],
    )

    attachments = db.relationship('Attachment', backref='note',
                                  cascade='all, delete-orphan')

    outbound = db.relationship(
        'Note', secondary=note_links,
        primaryjoin=id == note_links.c.src_id,
        secondaryjoin=id == note_links.c.dst_id,
        backref='inbound')

    def path(self):
        parts, n = [], self
        seen = set()
        while n and n.id not in seen:
            seen.add(n.id)
            parts.append(n.title)
            n = n.parent
        return ' / '.join(reversed(parts))

    def to_dict(self):
        return {'id': self.id, 'title': self.title, 'slug': self.slug}


class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    note_id = db.Column(db.Integer, db.ForeignKey('note.id'), nullable=False)
    filename = db.Column(db.String(300), nullable=False)
    original_name = db.Column(db.String(300), nullable=False)
    mimetype = db.Column(db.String(120))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_image(self):
        return (self.mimetype or '').startswith('image/')


class SavedSearch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    params = db.Column(db.String(1000), default='')
    sort_by = db.Column(db.String(20), default='modified')
    sort_dir = db.Column(db.String(4), default='desc')
    pinned = db.Column(db.Boolean, default=False)
    view_mode = db.Column(db.String(20), default='list')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Capture(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    kind = db.Column(db.String(20), nullable=False)  # 'new_child' | 'append_text' | 'attach_file' | 'daily_note'
    target_id = db.Column(db.Integer, db.ForeignKey('note.id'), nullable=False)

    # Fields used by 'new_child' captures
    title_template = db.Column(db.String(500), default='')
    body_template = db.Column(db.Text, default='')
    template_id = db.Column(db.Integer, db.ForeignKey('note.id'), nullable=True)

    # Field used by 'append_text', 'attach_file', and 'daily_note' captures
    after_action = db.Column(db.String(20), default='toast')

    # Optional prefix/suffix templates applied to the text/snippet that
    # gets written to the note. Empty string = no prefix/suffix.
    text_prefix = db.Column(db.String(500), default='')
    text_suffix = db.Column(db.String(500), default='')

    position = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    target = db.relationship('Note', foreign_keys=[target_id])
    template = db.relationship('Note', foreign_keys=[template_id])
    
class BlockMeta(db.Model):
    """Per-block metadata, keyed on (note_id, block_id).

    A "block" is a top-level markdown chunk (split on blank lines).
    Only blocks with metadata have a row here; plain blocks don't.
    """
    __tablename__ = 'block_meta'
    note_id = db.Column(db.Integer, db.ForeignKey('note.id', ondelete='CASCADE'),
                        primary_key=True)
    block_id = db.Column(db.String(16), primary_key=True)
    content_hash = db.Column(db.String(40))
    due_at = db.Column(db.DateTime)
    reminder_sent = db.Column(db.Boolean, default=False)
    metadata_json = db.Column(db.Text, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    note = db.relationship('Note', backref=db.backref('block_meta_rows',
                                                      lazy='dynamic'))    