import { useEffect, useState } from "preact/hooks";
import { useLocation } from "wouter-preact";

const PAGE_SUBTITLES: Record<string, string> = {
  "/": "Jobs",
  "/market": "Market",
  "/skills": "Skills",
  "/observability": "Model Observability & Token Inspector",
  "/resumes": "Resumes",
};

export const Header = () => {
  const [location] = useLocation();

  return (
    <header class="system-header-bar">
      <div class="system-title">
        <strong>Find a Job</strong> -{" "}
        {PAGE_SUBTITLES[location] ?? "Jobs"}
      </div>
      <Clock />
    </header>
  );
};

function formatRetroDate(d: Date): string {
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const yy = String(d.getFullYear() % 100).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const min = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${mm}-${dd}-${yy}  ${hh}:${min}:${ss}`;
}

const Clock = () => {
  const [clock, setClock] = useState(() => formatRetroDate(new Date()));

  useEffect(() => {
    const timer = setInterval(() => {
      setClock(formatRetroDate(new Date()));
    }, 1000);

    return () => clearInterval(timer);
  }, []);

  return <div class="system-clock">{clock}</div>;
};
