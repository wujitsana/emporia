import Text from "@kit/Text";
import { STYLE_ABOUT, STYLE_LABELS, STYLES, useStyleMode } from "../styleMode";

/**
 * Settings › Look: the style mode (styleMode.ts). A view preference kept in
 * this browser only — the one control in Settings that changes something,
 * because it changes nothing but how this page looks.
 */
export function StyleSwitch() {
  const [style, setStyle] = useStyleMode();
  return (
    <section className="e-settings-group e-style-switch" aria-labelledby="e-style-switch-title">
      <h3 className="e-settings-title" id="e-style-switch-title">Look · this browser only</h3>
      <div className="e-style-switch__options" role="radiogroup" aria-label="Style">
        {STYLES.map((s) => (
          <label key={s} className={`e-setting e-setting-ok e-style-switch__option${s === style ? " is-on" : ""}`}>
            <span className="e-setting-head">
              <input
                type="radio"
                name="emporia-style"
                value={s}
                checked={s === style}
                onChange={() => setStyle(s)}
              />
              <Text className="e-setting-label">{STYLE_LABELS[s]}</Text>
              {s === "retromod" && <Text className="e-setting-value">default</Text>}
            </span>
            <Text className="e-faint e-setting-about">{STYLE_ABOUT[s]}</Text>
          </label>
        ))}
      </div>
      <Text className="e-faint e-setting-note">
        Saved in this browser, never sent to a relay. <code>?style=animatic</code> tries it for one load;
        the <kbd>s</kbd> key switches it. Light or dark and the accent tint still apply.
      </Text>
    </section>
  );
}
