// Adapted from srcl (github.com/internet-development/www-sacred), MIT — see LICENSE-srcl.

import styles from './BlockLoader.module.css';

import * as React from 'react';

const SEQUENCES = [
  ['⠁', '⠂', '⠄', '⡀', '⢀', '⠠', '⠐', '⠈'],
  ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷'],
  ['▖', '▘', '▝', '▗'],
  ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█', '▇', '▆', '▅', '▄', '▃', '▁'],
  ['▉', '▊', '▋', '▌', '▍', '▎', '▏', '▎', '▍', '▌', '▋', '▊', '▉'],
  ['←', '↖', '↑', '↗', '→', '↘', '↓', '↙'],
  ['┤', '┘', '┴', '└', '├', '┌', '┬', '┐'],
  ['◢', '◣', '◤', '◥'],
  ['◰', '◳', '◲', '◱'],
  ['◴', '◷', '◶', '◵'],
  ['◐', '◓', '◑', '◒'],
];

interface BlockLoaderProps extends Omit<React.HTMLAttributes<HTMLSpanElement>, 'children'> {
  mode?: number;
}

const BlockLoader: React.FC<BlockLoaderProps> = ({ mode = 0 }) => {
  // Hooks first, every render (srcl called them after an early return).
  const [index, setIndex] = React.useState(0);
  const frames = SEQUENCES[mode];
  const length = frames ? frames.length : 0;

  React.useEffect(() => {
    if (!length) return;
    const timer = window.setInterval(() => setIndex((i) => (i + 1) % length), 100);
    return () => clearInterval(timer);
  }, [length]);

  if (!frames) {
    return <span className={styles.block}>�</span>;
  }
  return <span className={styles.root}>{frames[index % length]}</span>;
};

export default BlockLoader;
