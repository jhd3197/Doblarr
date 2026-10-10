import { useEffect, useRef, useState } from 'react';
import { lighten, palette, previewSample, SAMPLE, seedOf } from '../../lib/voices.js';
import { createOrb, primeAudio } from './orb.js';

// What the voices pages share: a voice's colours and orb, hearing a voice,
// the colour field and where a voice is cast.

export const voiceTitle = v => v.display_name || v.name;

export function dotStyle(v) {
  const [a, b] = palette(v);
  return { background: `radial-gradient(circle at 35% 30%, #fff 0%, ${a} 38%, ${b} 100%)` };
}

// The animated orb for a voice. `orbRef.current` is the live orb (follow,
// setColors) once mounted; without WebGL2 it shows a plain dot.
export function VoiceOrb({ voice, orbRef }) {
  const canvasRef = useRef(null);
  const key = voice.key;
  useEffect(() => {
    const canvas = canvasRef.current;
    let orb = null;
    // A shader that fails to build leaves the plain dot in the orb's place.
    try { orb = createOrb(canvas, { colors: palette(voice), seed: seedOf(key) }); } catch { canvas.className = 'voice-dot voice-dot-xl'; }
    if (orbRef) orbRef.current = orb;
    return () => { orb?.destroy(); if (orbRef) orbRef.current = null; };
  // A new voice gets a new orb; colour changes go through setColors.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return <canvas ref={canvasRef} className="voice-orb" aria-hidden="true" />;
}


// "Hear it": a sample in this voice, with the orb following the sound.
export function HearPanel({ voice, orbRef }) {
  const [text, setText] = useState(SAMPLE);
  const [lang, setLang] = useState((voice.language || 'es').slice(0, 2));
  const [status, setStatus] = useState('');
  const [src, setSrc] = useState('');
  const audioRef = useRef(null);
  const version = useRef(0);
  useEffect(() => () => { version.current++; }, []);

  async function speak() {
    const mine = ++version.current;
    try { primeAudio(); } catch { /* no Web Audio: the orb just idles */ }
    audioRef.current?.pause();
    setStatus('Generating…');
    try {
      const url = await previewSample({ key: voice.key, language: lang.trim().toLowerCase() || 'es',
        text: text.trim() || SAMPLE }, { current: () => mine === version.current });
      if (!url) return;
      setSrc(url);
      const audio = audioRef.current;
      audio.src = url;
      orbRef?.current?.follow(audio);
      await audio.play();
      setStatus('');
    } catch (error) { if (mine === version.current) setStatus(error.message); }
  }

  return (
    <section className="panel voice-panel" aria-labelledby="voiceHear">
      <h3 id="voiceHear">Hear it</h3>
      <div className="voice-preview-row">
        <textarea className="input voice-text" rows={2} maxLength={300} aria-label="Text to speak"
          value={text} onChange={e => setText(e.target.value)} />
        <label className="review-field">Language <input className="input m voice-lang" value={lang} maxLength={2} size={3}
          onChange={e => setLang(e.target.value)} /></label>
        <button type="button" className="btn btn-primary" data-speak onClick={speak}>Generate and play</button>
      </div>
      <audio ref={audioRef} className="voice-audio" controls hidden={!src} />
      <p className="hint" role="status" data-hear-status>{status}</p>
    </section>
  );
}

// A character colour: '' means automatic (from the voice key).
export function ColourField({ voice, value, onChange, orbRef }) {
  const shown = value || palette({ key: voice.key })[0];
  function pick(colour) {
    onChange(colour);
    orbRef?.current?.setColors(colour, lighten(colour));
  }
  function auto() {
    onChange('');
    const [c1, c2] = palette({ key: voice.key });
    orbRef?.current?.setColors(c1, c2);
  }
  return (
    <div className="review-field voice-color">Colour
      <span className="voice-color-row">
        <input type="color" className="voice-color-input" aria-label="Character colour" value={shown}
          onChange={e => pick(e.target.value)} />
        <span className="hint" data-color-note>{value || 'automatic'}</span>
        <button type="button" className="btn btn-ghost" data-color-auto hidden={!value} onClick={auto}>Automatic</button>
      </span>
    </div>
  );
}

const semis = (n, what) => (n ? `${n > 0 ? '+' : ''}${n} st ${what}` : '');

export function UsedPanel({ used }) {
  return (
    <section className="panel voice-panel" aria-labelledby="voiceUsed">
      <h3 id="voiceUsed">Cast in</h3>
      {used?.length ? (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Title</th><th>Speaker</th><th>Shaping</th></tr></thead>
          <tbody>{used.map((u, n) => (
            <tr key={n}><td>{u.title || u.title_key}</td><td className="m">{u.label || u.speaker || ''}</td>
              <td className="m">{[semis(u.pitch_semitones, 'pitch'), semis(u.formant_semitones, 'formant')].filter(Boolean).join(' · ') || '—'}</td></tr>
          ))}</tbody>
        </table></div>
      ) : <p className="hint">Not used in any saved dub yet.</p>}
    </section>
  );
}

export function VoiceTags({ voice }) {
  return (
    <span className="voice-tags">
      {voice?.gender && voice.gender !== 'unknown' && <span className="tag tag-neutral">{voice.gender}</span>}
      {voice?.age && voice.age !== 'unknown' && <span className="tag tag-neutral">{voice.age}</span>}
    </span>
  );
}
