// The one h1 of a top-level page: a title, a line saying what it is for, and
// optional controls (children) at the right.
export function PageHeader({ title, hint, children }) {
  return (
    <div className="page-head">
      <div><h1>{title}</h1>{hint && <p className="hint">{hint}</p>}</div>
      {children}
    </div>
  );
}
