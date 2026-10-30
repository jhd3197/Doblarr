import { useQuery } from '@tanstack/react-query';
import { languageName, targetChoices } from '../../lib/languages.js';
import { PLAN_FIELDS } from '../../lib/settings-model.js';
import { configQuery } from '../../lib/queries.js';

const configValue = (config, dotted) =>
  dotted.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), config);

// Text and number fields commit on change (blur or Enter), like a native
// "change" event, so typing doesn't save every keystroke.
function commitOnChange(commit) {
  return {
    onBlur: e => commit(e.target.value),
    onKeyDown: e => { if (e.key === 'Enter') commit(e.target.value); },
  };
}

function PlanField({ field: f, plan, config, targets, onSet, onError }) {
  const over = plan[f.k] !== undefined;
  let v;
  if (over) v = plan[f.k];
  else if (f.dyn) v = targets[0] || 'en';
  else { const c = configValue(config, f.k); v = c === undefined || c === null ? '' : c; }

  let control;
  if (f.t === 'json') {
    control = (
      <textarea className="input m" rows={4} aria-label={f.l} key={JSON.stringify(v || {})}
        defaultValue={JSON.stringify(v || {}, null, 2)}
        onBlur={e => {
          try {
            const value = JSON.parse(e.target.value);
            if (!value || Array.isArray(value) || typeof value !== 'object' || Object.values(value).some(x => typeof x !== 'string')) throw new Error();
            if (JSON.stringify(value) !== JSON.stringify(v || {})) onSet(f.k, value);
          } catch { onError('Enter a JSON object with text values.'); }
        }} />
    );
  } else if (f.t === 'text') {
    control = <input className="input m plan-text" type="text" key={String(v)} defaultValue={v}
      {...commitOnChange(value => { if (value !== String(v)) onSet(f.k, value); })} />;
  } else if (f.t === 'number') {
    // A blank field means "inherit", not "zero"; only a real number overrides.
    control = <input className="input m plan-number" type="number" step="any" key={String(v)} defaultValue={v}
      {...commitOnChange(value => { if (value !== '' && Number(value) !== Number(v)) onSet(f.k, Number(value)); })} />;
  } else {
    const options = f.t === 'bool' ? ['On', 'Off'] : (f.dyn ? targetChoices(targets, v) : f.o);
    control = (
      <div className="opts">
        {options.map(o => {
          const on = f.t === 'bool' ? (v ? 'On' : 'Off') === o : String(v) === o;
          const label = f.dyn ? languageName(o) : (o === '' && f.emptyLabel ? f.emptyLabel : o);
          return (
            <button key={o} type="button" className="opt" aria-pressed={on ? 'true' : 'false'}
              onClick={() => onSet(f.k, f.t === 'bool' ? o === 'On' : o)}>{label}</button>
          );
        })}
      </div>
    );
  }
  return (
    <div className="frow">
      <div>
        <div className="flabel">{f.l}</div>
        {over ? <div className="plan-flag"><span className="tag tag-accent">Overridden</span></div>
          : <div className="plan-flag plan-inherited">Inherited from Settings</div>}
      </div>
      <div>{control}{f.h && <p className="hint">{f.h}</p>}</div>
    </div>
  );
}

// Per-title dub plan: real config keys, so the overrides reach the pipeline
// (the worker deep-merges them over the global config for that job).
export function PlanTab({ item, targets, planState }) {
  const { data: config } = useQuery(configQuery);
  const { plan, status, setStatus, save, setValue } = planState;
  return (
    <div className="panel title-panel">
      <div className="title-panel-head">
        <h3>Dub plan for this title</h3>
        <p className="title-panel-sub">Anything you change here overrides your global settings for {item.title} only.</p>
        <span id="planStatus" className="plan-status">{status}</span>
        <button type="button" className="btn btn-ghost plan-reset" id="planReset" onClick={() => save({})}>Reset to global</button>
      </div>
      <div id="planFields" className="plan-fields">
        {!plan ? <p className="note">Loading plan…</p>
          : PLAN_FIELDS.map(f => <PlanField key={f.k} field={f} plan={plan} config={config} targets={targets}
              onSet={setValue} onError={setStatus} />)}
      </div>
    </div>
  );
}
