import { useState } from 'react';
import { Link, redirect, useParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { configQuery, ensure, hardwareQuery, languagesQuery, queryClient } from '../lib/queries.js';
import { api } from '../lib/api.js';
import { applyDeviceOptions, FIELD_BY_KEY, isBoolField, TABS } from '../lib/settings-model.js';
import { hardwareDetails } from '../lib/hardware.js';
import { PageHeader } from '../components/PageHeader.jsx';

const configValue = (config, dotted) =>
  dotted.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), config);

export async function settingsLoader({ params }) {
  if (!TABS.some(t => t.id === params.tab)) throw redirect(`/settings/${TABS[0].id}`);
  // Locale and device choices come from the language catalog and the hardware probe.
  const [config, , hw] = await Promise.all([ensure(configQuery), ensure(languagesQuery), ensure(hardwareQuery)]);
  applyDeviceOptions(hw, key => configValue(config, key));
  return null;
}

function HardwareInfo() {
  const { data: hw } = useQuery(hardwareQuery);
  async function refresh() {
    const report = await api('hardware?refresh=1').catch(() => null);
    const config = queryClient.getQueryData(['config']);
    applyDeviceOptions(report, key => configValue(config, key));
    queryClient.setQueryData(['hardware'], report);  // the sidebar and Overview card follow
  }
  return (
    <div className="m hw-info">
      {hardwareDetails(hw, hw?.memory).map(line => <div key={line}>{line}</div>)}
      <button type="button" className="btn btn-ghost hw-refresh" onClick={refresh}>Refresh</button>
    </div>
  );
}
const INFO = { hardware: HardwareInfo };

function Field({ field, value, showKeys, onChange }) {
  if (field.t === 'info') {
    const Info = INFO[field.id];
    return (
      <div className="frow">
        <div><div className="flabel">{field.l}</div></div>
        <div>{Info && <Info />}{field.h && <p className="hint">{field.h}</p>}</div>
      </div>
    );
  }
  let control = null;
  if (field.t === 'json') {
    control = <textarea className="input m" rows={4} aria-label={field.l}
      value={typeof value === 'string' ? value : JSON.stringify(value, null, 2)} onChange={e => onChange(e.target.value)} />;
  } else if (['text', 'number', 'list'].includes(field.t)) {
    control = <input className="input m settings-input" type={field.t === 'number' ? 'number' : 'text'}
      value={Array.isArray(value) ? value.join(', ') : value} onChange={e => onChange(e.target.value)} />;
  } else if (field.t === 'choice') {
    control = (
      <div className="opts">
        {field.o.map(o => (
          <button key={o} type="button" className="opt" aria-pressed={value === o ? 'true' : 'false'}
            onClick={() => onChange(o)}>{o === '' && field.emptyLabel ? field.emptyLabel : o}</button>
        ))}
      </div>
    );
  }
  return (
    <div className="frow">
      <div><div className="flabel">{field.l}</div>{showKeys && <div className="fkey">{field.k}</div>}</div>
      <div>{control}{field.h && <p className="hint">{field.h}</p>}</div>
    </div>
  );
}

// Edited values as the nested object POST /api/config takes, or an error message.
function buildPayload(vals) {
  const payload = {};
  for (const [k, v] of Object.entries(vals)) {
    const f = FIELD_BY_KEY[k];
    let val = f && isBoolField(f) ? v === 'On' : v;
    if (f?.t === 'number') val = Number(v);
    if (f?.t === 'list') val = String(v).split(',').map(s => s.trim()).filter(Boolean);
    if (f?.t === 'json') {
      try {
        val = JSON.parse(v);
        if (!val || Array.isArray(val) || typeof val !== 'object' || Object.values(val).some(x => typeof x !== 'string')) throw new Error();
      } catch { return { error: `${f.l}: enter a JSON object with text values.` }; }
    }
    const parts = k.split('.');
    let node = payload;
    parts.forEach((p, i) => { if (i === parts.length - 1) node[p] = val; else node = (node[p] = node[p] || {}); });
  }
  return { payload };
}

export function Settings() {
  const { tab } = useParams();
  const { data: config } = useQuery(configQuery);
  const [vals, setVals] = useState({});
  const [showKeys, setShowKeys] = useState(false);
  const [status, setStatus] = useState('');
  const current = TABS.find(t => t.id === tab) || TABS[0];

  function display(f) {
    if (vals[f.k] !== undefined) return vals[f.k];
    const c = configValue(config, f.k);
    if (c === undefined || c === null) return '';
    if (isBoolField(f)) return c ? 'On' : 'Off';
    return c;
  }

  async function save() {
    const { payload, error } = buildPayload(vals);
    if (error) { setStatus(error); return; }
    if (!Object.keys(payload).length) { setStatus('Nothing changed.'); return; }
    setStatus('Saving…');
    try {
      const data = await api('config', { method: 'POST', json: payload });
      queryClient.setQueryData(['config'], data.config);
      queryClient.invalidateQueries({ queryKey: ['library'] });  // connect.* may have changed
      setVals({});
      setStatus('Saved ✓');
    } catch (err) {
      setStatus('Save failed: ' + err.message);
    }
  }

  return (
    <div className="settings">
      <PageHeader title="Settings" hint="Connections, models and dubbing defaults, saved to config.yaml." />
      <div className="settings-tabs" id="settingsTabs">
        {TABS.map(t => (
          <Link key={t.id} to={`/settings/${t.id}`} className="tab" aria-current={t.id === current.id ? 'page' : 'false'}>{t.title}</Link>
        ))}
      </div>
      <div className="settings-body">
        <div className="settings-toolbar">
          <button type="button" className="opt" id="toggleKeys" aria-pressed={showKeys ? 'true' : 'false'}
            onClick={() => setShowKeys(!showKeys)}>Show config keys</button>
        </div>
        <div className="settings-groups" id="settingsGroups">
          {current.groups.map(g => (
            <div key={g.title} className="panel settings-group">
              <h3>{g.title}</h3>
              {g.desc && <p className="settings-desc">{g.desc}</p>}
              {g.fields.map(f => (
                <Field key={f.k || f.id} field={f} value={f.k ? display(f) : null} showKeys={showKeys}
                  onChange={v => setVals({ ...vals, [f.k]: v })} />
              ))}
            </div>
          ))}
        </div>
      </div>
      <div className="settings-footer">
        <button type="button" className="btn btn-secondary">Docs</button>
        <span className="m settings-file">config.yaml</span>
        <span id="saveStatus" className="settings-status">{status}</span>
        <button type="button" className="btn btn-primary" id="saveSettings" onClick={save}>Save changes</button>
      </div>
    </div>
  );
}
