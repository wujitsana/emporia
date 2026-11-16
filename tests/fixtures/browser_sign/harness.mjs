// Runs the signing page's script against fake DOM, window and wallet objects; prints what it did as JSON.
// usage: node harness.mjs <script file> <scenario json>
import fs from "node:fs";
import vm from "node:vm";

const [scriptFile, scenarioJson] = process.argv.slice(2);
const sc = JSON.parse(scenarioJson);   // {typed_data, chain_label, wallet: {chain, accounts, switch: "ok"|"refuse"|"unknown", sign: "ok"|"refuse"} | null, announce: bool, post: "ok"|"bad"}
const log = { calls: [], posts: [], status: "", statusClass: "", signDisabled: null, noWalletHidden: null };

const els = {};
const el = (id) => (els[id] ??= { id, textContent: "", className: "", hidden: false, disabled: false, value: "0", children: [], handlers: {},
  appendChild(c) { this.children.push(c); }, addEventListener(t, f) { this.handlers[t] = f; } });
el("page-data").textContent = JSON.stringify(sc.tx ? { tx: sc.tx, chain_label: sc.chain_label } : { typed_data: sc.typed_data, chain_label: sc.chain_label });

const winHandlers = {};
function makeWallet(w) {
  let chain = w.chain;
  return {
    async request({ method, params }) {
      log.calls.push(method);
      if (method === "eth_requestAccounts") return w.accounts;
      if (method === "eth_chainId") return "0x" + chain.toString(16);
      if (method === "wallet_switchEthereumChain") {
        if (w.switch === "refuse") throw { code: 4001, message: "User rejected" };
        if (w.switch === "unknown") throw { code: 4902, message: "Unrecognized chain" };
        chain = parseInt(params[0].chainId, 16);
        log.switched_to = chain;
        return null;
      }
      if (method === "eth_signTypedData_v4") {
        if (w.sign === "refuse") throw { code: 4001, message: "User rejected" };
        log.signed = { account: params[0], typed: JSON.parse(params[1]) };
        return "0x" + "ab".repeat(65);
      }
      if (method === "eth_sendTransaction") {
        if (w.send === "refuse") throw { code: 4001, message: "User rejected" };
        log.sent = params[0];
        return "0x" + "cd".repeat(32);
      }
      throw new Error("unexpected " + method);
    },
  };
}
const wallet = sc.wallet ? makeWallet(sc.wallet) : null;

const window = {
  addEventListener(t, f) { (winHandlers[t] ??= []).push(f); },
  dispatchEvent(e) {
    if (e.type === "eip6963:requestProvider" && wallet && sc.announce) {
      for (const f of winHandlers["eip6963:announceProvider"] ?? []) f({ detail: { info: { name: "Test Wallet" }, provider: wallet } });
    }
  },
  ethereum: wallet && !sc.announce ? wallet : undefined,
};
const context = vm.createContext({
  window, document: { getElementById: el, createElement: () => ({ value: "", textContent: "" }) },
  location: { pathname: "/TOKEN" }, Event: class { constructor(type) { this.type = type; } },
  setTimeout, JSON, Number, String, parseInt, Array, Promise,
  fetch: async (url, init) => {
    log.posts.push({ url, method: init.method, headers: init.headers, body: JSON.parse(init.body) });
    return { ok: sc.post !== "bad", text: async () => "refused" };
  },
});
vm.runInContext(fs.readFileSync(scriptFile, "utf8"), context);
for (const f of winHandlers.load ?? []) f();
await new Promise((r) => setTimeout(r, 500));   // the page waits briefly for a late wallet
log.signDisabledBeforeClick = el("sign").disabled;
log.noWalletHidden = el("nowallet").hidden;
if (!el("sign").disabled) {
  await el("sign").handlers.click();
}
log.status = el("status").textContent;
log.statusClass = el("status").className;
log.signDisabled = el("sign").disabled;
console.log(JSON.stringify(log));
