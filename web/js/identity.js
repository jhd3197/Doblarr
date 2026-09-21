function castParams(item) {
  const p = new URLSearchParams();
  if (item.episode_id && !item.path) p.set("key", `episode:${item.tvdb_id}:${item.episode_id}`);
  else if (item.path) p.set("path", item.path);
  else if (item.tmdb_id) p.set("tmdb_id", item.tmdb_id);
  else if (item.tvdb_id) p.set("tvdb_id", item.tvdb_id);
  else p.set("title", item.title);
  return p;
}

function jobsFor(item, jobs) {
  const norm = p => (p || "").replace(/\//g, "\\").toLowerCase();
  const ip = norm(item.path);
  if (!ip) return jobs.filter(j => j.title === item.title);
  const isFile = /\.[a-z0-9]+$/.test(ip);  // movies carry the file, shows the folder
  return jobs.filter(j => {
    const jp = norm(j.input_file);
    if (!jp) return j.title === item.title;
    return isFile ? jp === ip : (jp === ip || jp.startsWith(ip + "\\"));
  });
}


export { castParams, jobsFor };
