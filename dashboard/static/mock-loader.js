"use strict";
const mockReady = new URLSearchParams(location.search).get("mock") === "1"
  ? new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/static/mock.js";
    script.onload = resolve;
    script.onerror = () => reject(new Error("Could not load dashboard fixtures."));
    document.head.append(script);
  })
  : Promise.resolve();
