import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// Global tokens and base styles load first so component styles can refine them.
import "./styles/tokens.css";
import "./styles/base.css";
import { App } from "./App";

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
