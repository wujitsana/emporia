// Adapted from srcl (github.com/internet-development/www-sacred), MIT — see LICENSE-srcl.

import styles from './SidebarLayout.module.css';
import * as React from 'react';

interface SidebarLayoutProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'defaultValue'> {
  children?: React.ReactNode;
  sidebar?: React.ReactNode;
  defaultSidebarWidth?: number;
  isShowingHandle?: boolean;
  isReversed?: boolean;
  /** Narrow screens: which pane shows first. The list unless a deep link opened an item. */
  initialPane?: 'list' | 'detail';
  /** Narrow screens: the label of the control that returns to the list. */
  backLabel?: string;
}

const CHARACTER_WIDTH = 9.6;

/**
 * Rail beside detail on a wide screen. On a narrow one (see the module CSS) it is
 * list *then* detail: tapping a rail row (`.e-rail-item`) opens its detail on a
 * screen of its own, and a back control returns to the list. A row inside an
 * element marked `data-keep-list` (a filter, say) changes the list and stays on it.
 * Only what is shown changes — nothing here acts on the relay.
 */
const SidebarLayout: React.FC<SidebarLayoutProps> = ({ defaultSidebarWidth = 20, children, sidebar, isShowingHandle = false, isReversed = false, initialPane = 'list', backLabel = 'back to list', className, ...rest }) => {
  const [sidebarWidth, setSidebarWidth] = React.useState(defaultSidebarWidth);
  const [pane, setPane] = React.useState<'list' | 'detail'>(initialPane);
  const handleRef = React.useRef<HTMLDivElement>(null);
  const rootRef = React.useRef<HTMLDivElement>(null);

  const show = (next: 'list' | 'detail') => {
    setPane(next);
    // On a phone the page has usually scrolled down the list; start the new pane at its top.
    requestAnimationFrame(() => {
      const el = rootRef.current;
      if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start' });
    });
  };

  const onSidebarClick = (event: React.MouseEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement | null;
    if (target?.closest?.('.e-rail-item') && !target.closest('[data-keep-list]')) show('detail');
  };

  const handleMouseDown = (event: React.MouseEvent<HTMLDivElement>) => {
    const startX = event.clientX;
    const startWidth = sidebarWidth;

    const onMouseMove = (moveEvent: MouseEvent) => {
      const deltaX = moveEvent.clientX - startX;
      const increment = Math.round(deltaX / CHARACTER_WIDTH);
      setSidebarWidth(Math.max(CHARACTER_WIDTH, startWidth + increment));
    };

    const onMouseUp = () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  };

  const rootClass = [styles.root, pane === 'detail' ? styles.showDetail : styles.showList, className].filter(Boolean).join(' ');
  const sidebarEl = (
    <div className={styles.sidebar} style={{ width: `${sidebarWidth}ch` }} onClick={onSidebarClick} data-pane="list">
      {sidebar}
    </div>
  );
  const contentEl = (
    <div className={styles.content} data-pane="detail">
      <button type="button" className={styles.back} onClick={() => show('list')}>
        <span aria-hidden>←</span> {backLabel}
      </button>
      {children}
    </div>
  );

  if (isReversed) {
    return (
      <div className={rootClass} ref={rootRef} data-pane-shown={pane} {...rest}>
        {contentEl}
        {sidebarEl}
      </div>
    );
  }

  return (
    <div className={rootClass} ref={rootRef} data-pane-shown={pane} {...rest}>
      {sidebarEl}
      {isShowingHandle ? (
        <div className={styles.handle} ref={handleRef} role="button" tabIndex={0} onMouseDown={handleMouseDown}>
          <>
            <div className={styles.line} />
            <div className={styles.line} />
          </>
        </div>
      ) : null}
      {contentEl}
    </div>
  );
};

export default SidebarLayout;
