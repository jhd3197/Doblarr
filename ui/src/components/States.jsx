// The two shared "nothing to show" states. Both keep the quiet .note / .empty
// look; pass className for page-local spacing and extra props (ids, data-*).
export function Loading({ children = 'Loading…', className = '', ...rest }) {
  return <p role="status" className={`note ${className}`.trim()} {...rest}>{children}</p>;
}

// `block` is the boxed .empty used for lists and tables; default is a .note line.
export function EmptyState({ children, action, block = false, className = '', ...rest }) {
  return (
    <div className={`${block ? 'empty' : 'note'} ${className}`.trim()} {...rest}>
      {children}{action && <> {action}</>}
    </div>
  );
}
