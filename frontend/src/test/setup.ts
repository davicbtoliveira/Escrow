import { expect } from "bun:test";
import * as matchers from "@testing-library/jest-dom/matchers";
import { Window } from "happy-dom";

const window = new Window({ url: "http://localhost:5173" });

Object.assign(globalThis, {
  window,
  document: window.document,
  navigator: window.navigator,
  HTMLElement: window.HTMLElement,
  HTMLButtonElement: window.HTMLButtonElement,
  Event: window.Event,
  KeyboardEvent: window.KeyboardEvent,
  MouseEvent: window.MouseEvent,
  getComputedStyle: window.getComputedStyle.bind(window),
});

expect.extend(matchers);
