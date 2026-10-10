import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { api } from '../lib/api.js';
import { queryClient } from '../lib/queries.js';

// Queue a title by hand: what the library scan doesn't know about.
export function NewDubModal({ onClose }) {
  const navigate = useNavigate();
  const targets = queryClient.getQueryData(['library'])?.target_languages;
  const [form, setForm] = useState({ title: '', from: 'auto', to: targets?.[0] || 'en', path: '', kind: 'full' });
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const titleRef = useRef(null);
  const dialogRef = useRef(null);
  // A native modal dialog: focus stays inside it, Escape closes it, and focus
  // returns to whatever opened it.
  useEffect(() => {
    const opener = document.activeElement;
    dialogRef.current?.showModal();
    titleRef.current?.focus();
    return () => opener?.focus?.();
  }, []);
  const set = key => e => setForm({ ...form, [key]: e.target.value });

  async function queue() {
    const title = form.title.trim();
    if (!title) { titleRef.current?.focus(); return; }
    setBusy(true); setStatus('');
    try {
      await api('jobs', { method: 'POST', json: {
        title, source: 'manual', source_lang: form.from.trim() || 'auto', target_lang: form.to.trim() || 'en',
        path: form.path.trim() || null, kind: form.kind,
      } });
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
      onClose();
      navigate('/dubs');
    } catch (error) {
      setStatus(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog id="newDubModal" ref={dialogRef} className="modal-dialog" aria-labelledby="ndHeading"
      onClose={onClose} onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="panel modal-card">
        <h3 id="ndHeading" className="modal-title">New dub</h3>
        <p className="modal-sub">Queue a title manually.</p>
        <label className="modal-label" htmlFor="ndTitle">Title</label>
        <input id="ndTitle" ref={titleRef} className="input" type="text" placeholder="e.g. The Quiet Harbor"
          value={form.title} onChange={set('title')} />
        <div className="modal-pair">
          <div><label className="modal-label" htmlFor="ndFrom">From</label>
            <input id="ndFrom" className="input m" type="text" value={form.from} onChange={set('from')} /></div>
          <div><label className="modal-label" htmlFor="ndTo">To</label>
            <input id="ndTo" className="input m" type="text" value={form.to} onChange={set('to')} /></div>
        </div>
        <label className="modal-label" htmlFor="ndPath">File path <span className="modal-optional">(optional)</span></label>
        <input id="ndPath" className="input m" type="text" placeholder="M:/Plex/Movies/…/film.mkv"
          value={form.path} onChange={set('path')} />
        <label className="modal-label" htmlFor="ndKind">Output</label>
        <select id="ndKind" className="input" value={form.kind} onChange={set('kind')}>
          <option value="full">Full dubbed video</option>
          <option value="audition">Short voice audition (audio)</option>
          <option value="tease">Opening teaser (video)</option>
        </select>
        <div className="modal-actions">
          <button type="button" className="btn btn-secondary" id="ndCancel" onClick={onClose}>Cancel</button>
          <span id="ndStatus" role="alert" className="modal-status">{status}</span>
          <button type="button" className="btn btn-primary" id="ndQueue" disabled={busy} onClick={queue}>Queue dub</button>
        </div>
      </div>
    </dialog>
  );
}
