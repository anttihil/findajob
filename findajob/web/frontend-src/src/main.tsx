import { render } from "preact";
import { App } from "./App";
import { reportError } from "./api/client";
import "./style.css";

render(<App />, document.getElementById("app")!);

window.addEventListener("unhandledrejection", (event) => {
  reportError("Unhandled error", event.reason);
});
