#!/usr/bin/env node
/**
 * NovelAI 前端计费函数 Oracle
 *
 * 加载 novelai.net 的原始 webpack chunk（webpack.js / framework.js / main.js / _app.js），
 * 直接调用前端定价模块 23379 的 GI / tY / H_ 等函数，作为“网页真实逻辑”的参照。
 *
 * 用法:
 *   node oracle.js <input.json> <output.json>
 *
 * input.json 格式:
 *   {
 *     "cases": [
 *       {"id": 0, "fn": "GI", "args": [ {width:512,...}, {"subscription":{...}}, "nai-diffusion", false ]},
 *       {"id": 1, "fn": "tY", "args": [512, 768, {"subscription":{...}}]}
 *     ]
 *   }
 * output.json 格式:
 *   {"results": {"0": 5, "1": 1}}
 *
 * 支持函数: GI, tY, H_, ax, Jg (module 71810.ax / 18401.Jg), Dk
 */
"use strict";
const fs = require("fs");
const path = require("path");

function makeElement(tag) {
  return {
    tagName: (tag || "div").toUpperCase(), children: [], style: {}, dataset: {}, attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; },
    addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
    contains() { return false; }, closest() { return null; },
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 }; },
    insertBefore(c) { return c; }, querySelector() { return null; }, querySelectorAll() { return []; },
    focus() {}, click() {}, remove() {}, parentNode: null, isConnected: false,
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
  };
}
function makeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
    key: (i) => [...m.keys()][i] ?? null,
    get length() { return m.size; },
  };
}

// ---- 浏览器环境 mock（仅满足 webpack runtime 与无关模块的顶层初始化）----
const loc = {
  href: "https://novelai.net/image", protocol: "https:", host: "novelai.net",
  hostname: "novelai.net", port: "", pathname: "/image", search: "", hash: "",
  origin: "https://novelai.net", assign() {}, replace() {}, reload() {},
};
const doc = {
  baseURI: "https://novelai.net/image", location: loc,
  getElementsByTagName: () => [], createElement: (t) => makeElement(t),
  createElementNS: (ns, t) => makeElement(t), head: makeElement("head"), body: makeElement("body"),
  documentElement: makeElement("html"),
  addEventListener() {}, removeEventListener() {}, querySelector() { return null; },
  querySelectorAll() { return []; }, getElementById() { return null; },
  createDocumentFragment() { return makeElement("fragment"); },
  createTextNode: (t) => ({ textContent: t, nodeType: 3 }),
  createEvent: () => ({ initEvent() {} }),
  hasFocus: () => false, hidden: false, visibilityState: "visible", readyState: "complete", cookie: "",
};

global.self = global; global.window = global; global.document = doc; global.location = loc;
global.addEventListener = () => {}; global.removeEventListener = () => {};
Object.defineProperty(global, 'navigator', { value: {
  language: "en-US",
  userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
  platform: "Win32", onLine: true, clipboard: { writeText: async () => {} },
  cookieEnabled: true, sendBeacon: () => true, maxTouchPoints: 0,
}, configurable: true });
global.localStorage = makeStorage(); global.sessionStorage = makeStorage();
global.Element = function Element() {}; global.Node = function Node() {};
global.HTMLElement = function HTMLElement() {};
global.CSS = { supports: () => false };
global.screen = { width: 1920, height: 1080, orientation: { type: "landscape-primary" } };
global.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);
global.fetch = async () => { throw new Error("fetch not allowed in oracle"); };
global.XMLHttpRequest = function () { this.open = () => {}; this.send = () => {}; this.setRequestHeader = () => {}; this.abort = () => {}; };
global.Image = function () { this.addEventListener = () => {}; };
global.innerWidth = 1920; global.innerHeight = 1080; global.devicePixelRatio = 1;
global.postMessage = () => {};
global.history = { pushState() {}, replaceState() {}, back() {}, forward() {}, go() {} };

const cacheDir = path.join(__dirname, "cache");
function loadChunk(file) {
  return fs.readFileSync(path.join(cacheDir, file), "utf8");
}

// 1) webpack runtime
eval(loadChunk("webpack.js"));

// 2) 通过特殊 chunk 拿到 webpack require 对象
global.__wr = null;
self.webpackChunk_N_E.push([
  [0],
  { __oracle_hook__: () => {} },
  (wr) => { global.__wr = wr; },
]);
if (!global.__wr) throw new Error("无法获取 webpack require 对象");

// 3) 接管 push：只注册模块、不执行入口回调
self.webpackChunk_N_E.push = (chunk) => {
  const [, mods] = chunk;
  if (mods) for (const k in mods) global.__wr.m[k] = mods[k];
  return 0;
};

// 4) 注册各 chunk 的模块
//    1052 包含定价模块(61225: GI/tY/H_)与 Dk/尺寸模块(57863)
for (const f of ["framework.js", "main.js", "_app.js", "chunk-1052.js"]) {
  eval(loadChunk(f));
}

// 5) 取定价模块
const pricing = global.__wr(61225);
const model53856 = global.__wr(53856);
const subModule = global.__wr(62654);
const validation = global.__wr(57863);

const fns = {
  GI: (args) => pricing.GI(...args),
  tY: (args) => pricing.tY(...args),
  H_: (args) => pricing.H_(...args),
  Dk: (args) => validation.Dk(...args),
  ax: (args) => subModule.ax(...args),
  Jg: (args) => model53856.Jg(...args),
};

function main() {
  const [inputPath, outputPath] = process.argv.slice(2);
  if (!inputPath || !outputPath) {
    console.error("用法: node oracle.js <input.json> <output.json>");
    process.exit(2);
  }
  const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));
  const results = {};
  const errors = {};
  for (const c of input.cases) {
    const fn = fns[c.fn];
    if (!fn) { errors[c.id] = "unknown fn " + c.fn; continue; }
    try {
      const v = fn(c.args);
      results[c.id] = (typeof v === "number" && Object.is(v, -0)) ? 0 : v;
    } catch (e) {
      errors[c.id] = e && e.message ? e.message : String(e);
    }
  }
  fs.writeFileSync(outputPath, JSON.stringify({ results, errors }, null, 2), "utf8");
}

main();
