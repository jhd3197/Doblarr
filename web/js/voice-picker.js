import { api, apiUrl } from './api.js';
import { escapeHtml as esc, safeGet } from './dom.js';

export async function pickVoice({ language, category = 'speaker', onSelect }) {
  const dialog = document.createElement('dialog');
  dialog.className = 'voice-picker';
  document.getElementById('app').append(dialog);
  const opener = document.activeElement;
  let voices = [], timer, previewVersion = 0, query = '', source = 'all', limit = 24;
  let desiredAge = category.startsWith('elderly') ? 'older' : category.startsWith('child') ? 'child' : category.startsWith('young') ? 'young' : 'unknown';
  let desiredGender = category.endsWith('_m') ? 'male' : category.endsWith('_f') ? 'female' : 'unknown';
  let onlyLanguage = false;
  dialog.innerHTML = `<header><div><h2>Choose a character voice</h2><p class="hint">Saved voices and presets exposed by your Voicebox engines. Age is a listening tag, not an automatic identity estimate.</p></div><button class="btn btn-ghost picker-close">Close</button></header>
    <div class="voice-filters"><label>Search<input class="input picker-search" type="search"></label>
    <label>Character age<select class="input picker-age" aria-label="Character age">${['unknown','child','young','adult','older'].map(a => `<option value="${a}">${a === 'unknown' ? 'Any age' : a}</option>`).join('')}</select></label>
    <label>Voice gender<select class="input picker-gender" aria-label="Voice gender">${['unknown','male','female','neutral'].map(a => `<option value="${a}">${a === 'unknown' ? 'Any voice' : a}</option>`).join('')}</select></label>
    <label>Catalog<select class="input picker-source" aria-label="Catalog"><option value="all">All voices</option><option value="profile">Saved voices</option><option value="kokoro">Kokoro</option><option value="qwen_custom_voice">Qwen CustomVoice</option></select></label>
    <label><input class="picker-language" type="checkbox"> ${esc(language.toUpperCase())} native voices and clones only</label></div>
    <div class="voice-preview"><label>Audition text<input class="input picker-text" maxlength="300" value="${esc(language === 'es' ? 'Al caer la noche, el anciano comenzó a contar su historia.' : 'As night fell, the old man began to tell his story.')}"></label>
    <label>Delivery direction (Qwen)<input class="input picker-direction" maxlength="500" placeholder="For example: a low, weathered older voice; calm and thoughtful"></label><audio controls class="picker-audio" hidden></audio>
    <p class="picker-status" role="status">Loading catalog…</p></div><div class="voice-results"></div><button class="btn btn-secondary picker-more" hidden>Show more voices</button>`;
  dialog.showModal();
  dialog.querySelector('.picker-close').onclick = () => dialog.close();
  dialog.addEventListener('close', () => { clearTimeout(timer); previewVersion++; dialog.querySelector('audio').pause(); dialog.remove(); opener?.focus(); });
  const status = dialog.querySelector('.picker-status');
  function rank(v) {
    return (v.language === language ? 8 : 0) + (desiredGender !== 'unknown' && v.gender === desiredGender ? 4 : 0)
      + (desiredAge !== 'unknown' && v.age === desiredAge ? 8 : 0);
  }
  function render() {
    const rows = voices.filter(v => (!query || `${v.name} ${v.description} ${v.engine}`.toLowerCase().includes(query))
      && (source === 'all' || (source === 'profile' ? v.key.startsWith('profile:') : v.engine === source))
      && (!onlyLanguage || v.language === language || v.kind === 'cloned')
      && (desiredGender === 'unknown' || v.gender === desiredGender || v.gender === 'unknown')
      && (desiredAge === 'unknown' || v.age === desiredAge || v.age === 'unknown'))
      .sort((a,b) => rank(b)-rank(a) || a.name.localeCompare(b.name));
    dialog.querySelector('.voice-results').innerHTML = `<p class="hint">${rows.length} matching voices · ${voices.length} in the catalog. Untagged voices remain candidates; audition to confirm the character fit.</p>`
      + rows.slice(0, limit).map(v => `<article class="voice-card" data-key="${esc(v.key)}"><div><h3>${esc(v.name)}</h3><p class="hint">${esc(v.engine)} · ${esc(v.language.toUpperCase())} · ${esc(v.gender)} · ${v.age === 'unknown' ? 'Age not tagged' : esc(v.age)}</p><p>${esc(v.description)}</p></div>
      <label>Age after listening<select class="input voice-age" aria-label="Age tag for ${esc(v.name)}">${['unknown','child','young','adult','older'].map(a => `<option value="${a}" ${v.age === a ? 'selected' : ''}>${a}</option>`).join('')}</select></label>
      <label>Voice gender tag<select class="input voice-gender" aria-label="Gender tag for ${esc(v.name)}">${['unknown','male','female','neutral'].map(g=>`<option value="${g}" ${g===v.gender?'selected':''}>${g}</option>`).join('')}</select></label>
      ${v.engine==='kokoro' && v.language!==language ? `<p class="hint">This Kokoro preset is for ${esc(v.language.toUpperCase())}; choose a ${esc(language.toUpperCase())} preset for this dub.</p>` : ''}
      <div class="episode-actions"><button class="btn btn-secondary voice-listen" ${v.engine==='kokoro' && v.language!==language?'disabled':''}>Generate sample</button><button class="btn btn-primary voice-use" ${v.engine==='kokoro' && v.language!==language?'disabled':''}>Use this voice</button></div></article>`).join('');
    dialog.querySelector('.picker-more').hidden = rows.length <= limit;
    dialog.querySelectorAll('.voice-card').forEach(card => {
      const voice = voices.find(v => v.key === card.dataset.key);
      const saveTraits = async () => {
        try { await api('voice-catalog/traits', { method:'PUT', json:{key:voice.key, age:card.querySelector('.voice-age').value, gender:card.querySelector('.voice-gender').value} }); voice.age=card.querySelector('.voice-age').value; voice.gender=card.querySelector('.voice-gender').value; status.textContent='Voice traits saved.'; }
        catch(error) { status.textContent=error.message; }
      };
      card.querySelector('.voice-age').onchange=saveTraits; card.querySelector('.voice-gender').onchange=saveTraits;
      card.querySelector('.voice-use').onclick = async e => {
        e.target.disabled=true;
        try {
          const selected = await api('voice-catalog/select', {method:'POST',json:{key:voice.key}});
          onSelect({...selected, direction: ['qwen','qwen_custom_voice'].includes(selected.engine) ? dialog.querySelector('.picker-direction').value : ''});
          dialog.close();
        } catch(error) { status.textContent=error.message; e.target.disabled=false; }
      };
      card.querySelector('.voice-listen').onclick = () => preview(voice);
    });
  }
  async function preview(voice) {
    const version = ++previewVersion; clearTimeout(timer);
    const audio = dialog.querySelector('audio'); audio.pause(); audio.hidden=true;
    status.textContent='Generating a short sample…';
    try {
      const response = await api('voice-catalog/preview', {method:'POST',json:{key:voice.key,language,
        text:dialog.querySelector('.picker-text').value,
        direction:['qwen','qwen_custom_voice'].includes(voice.engine) ? dialog.querySelector('.picker-direction').value : ''}});
      const deadline=Date.now()+180000;
      async function poll() {
        if (!dialog.open || version !== previewVersion) return;
        try {
          const result=await api(`voice-catalog/preview/${response.id}`);
          if (!dialog.open || version !== previewVersion) return;
          if (['completed','done','ready','success'].includes(result.status)) {
            const key=safeGet('doblarr_api_key','');
            audio.src=apiUrl(`voice-catalog/preview/${response.id}/audio`)+(key?`?api_key=${encodeURIComponent(key)}`:'');
            audio.hidden=false; status.textContent=`${voice.name}: sample ready. Listen before assigning.`;
          } else if (['failed','error','cancelled','canceled'].includes(result.status)) status.textContent=result.error || 'Voice sample failed.';
          else if (Date.now()>deadline) status.textContent='Still processing in Voicebox. Check its history before starting another sample.';
          else timer=setTimeout(poll,1500);
        } catch(error) { if(dialog.open) status.textContent=error.message; }
      }
      poll();
    } catch(error) { if(dialog.open && version===previewVersion) status.textContent=error.message; }
  }
  dialog.querySelector('.picker-age').value=desiredAge;
  dialog.querySelector('.picker-gender').value=desiredGender;
  if(desiredAge==='older') dialog.querySelector('.picker-direction').value=`Speak as an older ${desiredGender==='male'?'man':desiredGender==='female'?'woman':'adult'}, with a naturally weathered tone and measured pacing.`;
  dialog.querySelector('.picker-search').oninput=e=>{query=e.target.value.toLowerCase();limit=24;render();};
  dialog.querySelector('.picker-age').onchange=e=>{desiredAge=e.target.value;render();};
  dialog.querySelector('.picker-gender').onchange=e=>{desiredGender=e.target.value;render();};
  dialog.querySelector('.picker-source').onchange=e=>{source=e.target.value;render();};
  dialog.querySelector('.picker-language').onchange=e=>{onlyLanguage=e.target.checked;render();};
  dialog.querySelector('.picker-more').onclick=()=>{limit+=24;render();};
  try { const result=await api('voice-catalog');voices=result.voices; if(dialog.open){status.textContent=result.warnings.join('; ');render();} }
  catch(error){if(dialog.open)status.textContent=error.message;}
}
