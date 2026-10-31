import { useEffect, useRef, useState } from 'react';
import { settleDialog, useDialog } from '../lib/dialogs.js';

// Mounted once in the shell. A native modal <dialog>: focus stays inside,
// Escape cancels, and focus returns to whatever opened it.
export function DialogHost() {
  const dialog = useDialog();
  return dialog ? <OpenDialog key={dialog.id} dialog={dialog} /> : null;
}

function OpenDialog({ dialog }) {
  const ref = useRef(null);
  const inputRef = useRef(null);
  const [value, setValue] = useState(dialog.defaultValue ?? '');
  useEffect(() => {
    const opener = document.activeElement;
    ref.current?.showModal();
    inputRef.current?.select();
    return () => opener?.focus?.();
  }, []);
  const submit = e => { e.preventDefault(); settleDialog(dialog.kind === 'prompt' ? value : true); };
  return (
    <dialog ref={ref} className="modal-dialog" id="appDialog" aria-labelledby="appDialogTitle"
      onCancel={e => { e.preventDefault(); settleDialog(); }}
      onClick={e => { if (e.target === e.currentTarget) settleDialog(); }}>
      <form className="panel modal-card" onSubmit={submit}>
        <h3 id="appDialogTitle" className="modal-title">{dialog.title}</h3>
        {dialog.message && <p className="modal-sub">{dialog.message}</p>}
        {dialog.kind === 'prompt' && (
          <label className="modal-label">{dialog.label || dialog.title}
            <input ref={inputRef} className="input" type="text" value={value} onChange={e => setValue(e.target.value)} />
          </label>
        )}
        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={() => settleDialog()}>Cancel</button>
          <button type="submit" className={dialog.danger ? 'btn btn-danger' : 'btn btn-primary'}>{dialog.confirmLabel}</button>
        </div>
      </form>
    </dialog>
  );
}
