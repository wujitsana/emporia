// Adapted from srcl (github.com/internet-development/www-sacred), MIT — see LICENSE-srcl.
import styles from './Text.module.css';

import * as React from 'react';

interface TextProps extends React.HTMLAttributes<HTMLParagraphElement> {
  children?: React.ReactNode;
}

const Text: React.FC<TextProps> = ({ children, ...rest }) => {
  return (
    <p className={styles.text} {...rest}>
      {children}
    </p>
  );
};

export default Text;
