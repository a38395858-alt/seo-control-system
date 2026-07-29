import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// The established workflow owns the live keyword, title, content, source,
// image and publishing operations.  Keep it as the application entry while
// the redesigned project shell is progressively composed from these surfaces.
import App from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
