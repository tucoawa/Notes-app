import * as CM from 'codemirror-bundle';

const {
  EditorState,
  EditorView,
  keymap,
  Decoration,
  WidgetType,
  ViewPlugin,
  placeholder,
  defaultKeymap,
  history,
  historyKeymap,
  undo,
  redo,
  indentWithTab,
  markdown,
  syntaxHighlighting,
  defaultHighlightStyle,
  HighlightStyle,
  tags,               
} = CM;

// ---------- Widgets ----------

class ImageWidget extends WidgetType {
  constructor(url, alt) { super(); this.url = url; this.alt = alt; }
  eq(other) { return other.url === this.url && other.alt === this.alt; }
  toDOM() {
    const wrap = document.createElement('span');
    wrap.className = 'cm-image-widget';
    const img = document.createElement('img');
    img.src = this.url;
    img.alt = this.alt || '';
    img.loading = 'lazy';
    wrap.appendChild(img);
    return wrap;
  }
  ignoreEvent() { return true; }
  get lineBreaks() { return 0; }
  get estimatedHeight() { return -1; }
}

class WikiLinkWidget extends WidgetType {
  constructor(title, blockQuery) { 
    super(); 
    this.title = title; 
    this.blockQuery = blockQuery; 
  }
  eq(other) { 
    return other.title === this.title && other.blockQuery === this.blockQuery; 
  }
  toDOM() {
    const a = document.createElement('a');
    a.className = 'cm-wikilink-widget';
    
    // If there's a block, show "Note Title > Block"
    if (this.blockQuery) {
      a.textContent = `${this.title} > ${this.blockQuery}`;
      a.title = `Open ${this.title} at block: ${this.blockQuery}`;
    } else {
      a.textContent = this.title;
      a.title = 'Open note: ' + this.title;
    }

    a.href = '#';
    a.addEventListener('mousedown', (e) => {
      e.preventDefault();
      e.stopPropagation();
    });
    a.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      
      const fullTitle = this.blockQuery 
        ? `${this.title}#${this.blockQuery}` 
        : this.title;
        
      const r = await fetch(`/api/notes/by-title/${encodeURIComponent(fullTitle)}`);
      if (r.ok) {
        const n = await r.json();
        if (window.__flushSave) {
          try {
            await window.__flushSave();
          } catch (err) {
            console.warn('Save failed before wikilink navigation:', err);
          }
        }
        let url = `/?note=${n.id}`;
        if (n.block_id) url += `&block=${n.block_id}`;
        location.href = url;
      } else {
        alert('Note not found: ' + this.title);
      }
    });
    return a;
  }
  ignoreEvent() { return false; }
}

// Block-marker widget: hides `<!--blk:xxxxxxxx-->` in the editor
// source view. The marker stays in the doc string (so it saves), but
// visually takes no space and can't be edited character-by-character.
class BlockMarkerWidget extends WidgetType {
  constructor(id) { super(); this.id = id; }
  eq(other) { return other.id === this.id; }
  toDOM() {
    const span = document.createElement('span');
    span.className = 'cm-block-marker-hidden';
    span.setAttribute('data-block-id', this.id);
    span.title = 'Block marker (invisible)';
    return span;
  }
  ignoreEvent() { return true; }
  get lineBreaks() { return 0; }
  get estimatedHeight() { return -1; }   // let CM measure actual height
  get atomic() { return true; }           // prevent cursor from entering
}

// ---------- Decoration plugin ----------

function buildDecorations(view) {
  const widgets = [];
  const text = view.state.doc.toString();

  const imgRe = /!\[([^\]]*)\]\(([^)\s]+)\)/g;
  let m;
  while ((m = imgRe.exec(text)) !== null) {
    const from = m.index;
    const to = from + m[0].length;
    if (text.slice(from, to).includes('\n')) continue;
    widgets.push({
      from, to,
      deco: Decoration.replace({
        widget: new ImageWidget(m[2], m[1]),
        block: false,
      })
    });
  }

  const wikiRe = /(?:@)?\[\[([^\]#]+)(?:#([^\]]+))?\]\]/g;
  while ((m = wikiRe.exec(text)) !== null) {
    const from = m.index;
    const to = from + m[0].length;
    if (from > 0 && text[from - 1] === '!') continue;
    if (text.slice(from, to).includes('\n')) continue;
    if (widgets.some(w => !(to <= w.from || from >= w.to))) continue;
    widgets.push({
      from, to,
      deco: Decoration.replace({
        widget: new WikiLinkWidget(m[1], m[2]), // <--- Pass m[2] as blockQuery
        block: false,
      })
    });
  }

  // Block markers: `<!--blk:xxxxxxxx-->`
  // We always hide the marker text, regardless of surrounding content.
  // `atomic: true` tells CodeMirror to treat the widget as an
  // indivisible unit — the cursor can sit before or after it, but
  // never "inside" it. This prevents typing from corrupting the marker.
  const markerRe = /<!--blk:([0-9a-f]{8})-->/g;
  while ((m = markerRe.exec(text)) !== null) {
    const from = m.index;
    const to = from + m[0].length;
    if (widgets.some(w => !(to <= w.from || from >= w.to))) continue;

    widgets.push({
      from, to,
      deco: Decoration.replace({
        widget: new BlockMarkerWidget(m[1]),
        block: false,
        atomic: true,     // ← ADD
      })
    });
  }

  widgets.sort((a, b) => a.from - b.from);
  return Decoration.set(widgets.map(w => w.deco.range(w.from, w.to)));
}

const widgetPlugin = ViewPlugin.fromClass(class {
  constructor(view) {
    this.decorations = buildDecorations(view);
  }
  update(update) {
    if (update.docChanged || update.viewportChanged) {
      this.decorations = buildDecorations(update.view);
    }
  }
}, {
  decorations: v => v.decorations,
});

// ---------- Factory ----------

export function createEditor(parent, initialDoc, onChange, onFocus) {
  if (initialDoc && !initialDoc.endsWith('\n')) initialDoc = initialDoc + '\n';
  let silent = false;
  const state = EditorState.create({
    doc: initialDoc || '',
    extensions: [
      history(),
      placeholder('Write ...'),
      markdown(),
      syntaxHighlighting(HighlightStyle.define([])),
      keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
      EditorView.lineWrapping,
      widgetPlugin,
      EditorView.updateListener.of((update) => {
        if (update.docChanged && !silent) onChange();
      }),
      EditorView.domEventHandlers({
        focus: () => { if (onFocus) onFocus(); },
      }),
      EditorView.theme({
        '&': {
          fontSize: '16px',
          fontFamily: "'Roboto', system-ui, sans-serif",
          color: 'var(--text)',
          backgroundColor: 'transparent',
        },
        '.cm-content': {
          caretColor: 'var(--accent)',
          padding: '0 !important',
          minHeight: '100%',
          fontFamily: 'inherit',
        },
        '.cm-scroller': {
          fontFamily: 'inherit',
          lineHeight: '1.65',
          padding: '0',
          paddingTop: '0px',
          overflow: 'auto',
        },
        '.cm-cursor': { borderLeftColor: 'var(--accent)' },
        '&.cm-focused': { outline: 'none' },
        '.cm-line': { padding: '0 4px !important' },
      }),
    ],
  });

  const view = new EditorView({ state, parent });

  return {
    view,
    getContent: () => view.state.doc.toString(),
    setContent: (text) => {
      if (text && !text.endsWith('\n')) text = text + '\n';
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: text },
      });
    },
    setContentSilent: (text) => {
      if (text && !text.endsWith('\n')) text = text + '\n';
      silent = true;
      try {
        view.dispatch({
          changes: { from: 0, to: view.state.doc.length, insert: text },
        });
      } finally {
        silent = false;
      }
    },
    focus: () => view.focus(),
    undo: () => { undo(view); view.focus(); },
    redo: () => { redo(view); view.focus(); },
    getSelection: () => {
      const sel = view.state.selection.main;
      return { start: sel.from, end: sel.to };
    },
    insertAtCursor: (text) => {
      const sel = view.state.selection.main;
      view.dispatch({
        changes: { from: sel.from, to: sel.to, insert: text },
        selection: { anchor: sel.from + text.length },
      });
      view.focus();
    },
    insertSmart: (text, preferCursor) => {
      if (preferCursor) {
        const sel = view.state.selection.main;
        view.dispatch({
          changes: { from: sel.from, to: sel.to, insert: text },
          selection: { anchor: sel.from + text.length },
        });
      } else {
        const current = view.state.doc.toString();
        const trimmedRight = current.replace(/\s+$/, '');
        const insert = trimmedRight.length > 0 
          ? '\n\n' + text 
          : text;
        view.dispatch({
          changes: { from: 0, to: current.length, insert: trimmedRight + insert },
          selection: { anchor: trimmedRight.length + insert.length },
          effects: EditorView.scrollIntoView(trimmedRight.length + insert.length, { y: 'end' }),
        });
        return;
      }
      view.focus();
    },
    replaceRange: (from, to, text) => {
      view.dispatch({
        changes: { from, to, insert: text },
        selection: { anchor: from + text.length },
      });
      view.focus();
    },
  };
}