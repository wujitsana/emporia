// Adapted from srcl (github.com/internet-development/www-sacred), MIT — see LICENSE-srcl.
import styles from './Indent.module.css';

import * as React from 'react';

interface IndentProps extends React.HTMLAttributes<HTMLDivElement> {
  children?: React.ReactNode;
}

const Indent: React.FC<IndentProps> = ({ children, ...rest }) => {
  return (
    <div className={styles.root} {...rest}>
      {children}
    </div>
  );
};

export default Indent;
