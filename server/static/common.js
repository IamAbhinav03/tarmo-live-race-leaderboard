const Tarmo = (() => {
  const formatTime = (microseconds) => {
    if (microseconds == null) return "--:--.---";
    const totalMs = Math.max(0, Math.round(Number(microseconds) / 1000));
    const minutes = Math.floor(totalMs / 60000);
    const seconds = Math.floor((totalMs % 60000) / 1000);
    const millis = totalMs % 1000;
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
  };

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  const shortDate = (iso) => {
    if (!iso) return "—";
    return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" }).format(new Date(iso));
  };

  const formatDistance = (millimetres) => `${(Number(millimetres) / 1000).toFixed(2)} m`;
  const formatFeet = (millimetres) => `${(Number(millimetres) / 304.8).toFixed(1)} ft`;

  const connect = (render) => {
    let polling;
    const apply = (state) => render(state);
    const fetchState = () => fetch("/api/state", { cache: "no-store" }).then((r) => r.json()).then(apply);
    fetchState().catch(console.error);

    const events = new EventSource("/api/events");
    events.addEventListener("state", (event) => {
      clearInterval(polling);
      apply(JSON.parse(event.data));
    });
    events.onerror = () => {
      if (!polling) polling = setInterval(() => fetchState().catch(console.error), 2500);
    };
    return fetchState;
  };

  return { formatTime, formatDistance, formatFeet, escapeHtml, shortDate, connect };
})();
