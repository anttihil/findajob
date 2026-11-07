import { useState } from "preact/hooks";
import { postJSON } from "../api/client";
import type { TargetLocation } from "../api/types";
import { LOCATION_PRESETS } from "../lib/locationPresets";

interface LocationPickerProps {
  locations: TargetLocation[];
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  onLocationSaved: () => Promise<void>;
  disabled?: boolean;
  onBusyChange: (busy: boolean) => void;
}

export function LocationPicker({
  locations,
  selectedIds,
  onChange,
  onLocationSaved,
  disabled = false,
  onBusyChange,
}: LocationPickerProps) {
  const [search, setSearch] = useState("");
  const [custom, setCustom] = useState(false);
  const [label, setLabel] = useState("");
  const [searchLabel, setSearchLabel] = useState("");
  const [country, setCountry] = useState("US");
  const [indeedCountry, setIndeedCountry] = useState("usa");
  const [remote, setRemote] = useState(false);
  const [distance, setDistance] = useState(50);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const matches = (value: string) => value.toLowerCase().includes(search.trim().toLowerCase());
  const recent = locations.filter((loc) => loc.last_used_at).slice(0, 10);
  const recentIds = new Set(recent.map((loc) => loc.id));
  const other = locations.filter((loc) => !recentIds.has(loc.id));
  const presets = LOCATION_PRESETS.filter(
    (preset) =>
      matches(`${preset.label} ${preset.country}`) &&
      !locations.some(
        (loc) =>
          loc.search_label === preset.search_label &&
          loc.country === preset.country &&
          Boolean(loc.is_remote) === preset.is_remote &&
          loc.distance === preset.distance &&
          loc.indeed_country === preset.indeed_country,
      ),
  );

  const toggle = (id: string) =>
    onChange(
      selectedIds.includes(id) ? selectedIds.filter((value) => value !== id) : [...selectedIds, id],
    );

  const addLocation = async (location: {
    id: string;
    label: string;
    search_label: string;
    country: string;
    indeed_country: string;
    is_remote: boolean;
    distance: number;
  }) => {
    if (saving) return;
    setSaving(true);
    onBusyChange(true);
    setError("");
    try {
      const result = await postJSON<{ id: string }>("/api/targets/locations", location);
      await onLocationSaved();
      if (!selectedIds.includes(result.id)) onChange([...selectedIds, result.id]);
      setCustom(false);
      setSearch("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save location. Please try again.");
    } finally {
      setSaving(false);
      onBusyChange(false);
    }
  };

  const renderLocation = (loc: TargetLocation) => (
    <label class="location-picker-option" key={loc.id}>
      <input
        type="checkbox"
        checked={selectedIds.includes(loc.id)}
        disabled={saving}
        onChange={() => toggle(loc.id)}
      />
      <span>
        <strong>{loc.label}</strong>
        <small>
          {loc.country} · {loc.is_remote ? "Remote" : `${loc.distance} mi radius`}
        </small>
      </span>
    </label>
  );

  return (
    <fieldset class="location-picker" disabled={saving || disabled}>
      <legend>Locations</legend>
      <p class="form-hint">Choose one or more locations for this query.</p>
      {selectedIds.length > 0 && (
        <div class="location-picker-selected">
          {selectedIds.map((id) => (
            <button
              type="button"
              class="btn btn-xs btn-outline"
              key={id}
              aria-label={`Remove ${locations.find((loc) => loc.id === id)?.label || id}`}
              onClick={() => toggle(id)}
            >
              {locations.find((loc) => loc.id === id)?.label || id} ×
            </button>
          ))}
        </div>
      )}
      <input
        type="search"
        class="form-input"
        aria-label="Find a location"
        placeholder="Find a location…"
        value={search}
        onInput={(e) => setSearch(e.currentTarget.value)}
      />
      <div class="location-picker-options">
        {recent.some((loc) => matches(`${loc.label} ${loc.country}`)) && <h4>Recently used</h4>}
        {recent.filter((loc) => matches(`${loc.label} ${loc.country}`)).map(renderLocation)}
        {other.some((loc) => matches(`${loc.label} ${loc.country}`)) && <h4>Saved locations</h4>}
        {other.filter((loc) => matches(`${loc.label} ${loc.country}`)).map(renderLocation)}
        {presets.length > 0 && <h4>Suggested locations</h4>}
        {presets.map((preset) => (
          <button
            type="button"
            class="location-picker-preset"
            key={preset.id}
            onClick={() => void addLocation(preset)}
          >
            + {preset.label}
          </button>
        ))}
      </div>
      <button type="button" class="btn btn-sm btn-outline" onClick={() => setCustom(!custom)}>
        {custom ? "Cancel new location" : "New custom location"}
      </button>
      {custom && (
        <div class="location-picker-custom">
          <label>
            Location name
            <input
              class="form-input"
              value={label}
              onInput={(e) => setLabel(e.currentTarget.value)}
              placeholder="e.g. Portland, OR"
            />
          </label>
          <details>
            <summary>Search settings</summary>
            <div class="form-grid-2col">
              <label>
                Search location
                <input
                  class="form-input"
                  value={searchLabel}
                  onInput={(e) => setSearchLabel(e.currentTarget.value)}
                  placeholder={label || "City, region or country"}
                />
              </label>
              <label>
                Country code
                <input
                  class="form-input"
                  value={country}
                  maxLength={2}
                  onInput={(e) => setCountry(e.currentTarget.value.toUpperCase())}
                />
              </label>
              <label>
                Indeed country
                <input
                  class="form-input"
                  value={indeedCountry}
                  onInput={(e) => setIndeedCountry(e.currentTarget.value)}
                />
              </label>
              <label>
                Radius (miles)
                <input
                  type="number"
                  class="form-input"
                  value={distance}
                  min={5}
                  max={250}
                  onInput={(e) => setDistance(Number(e.currentTarget.value))}
                />
              </label>
            </div>
          </details>
          <label class="checkbox-label">
            <input
              type="checkbox"
              checked={remote}
              onChange={(e) => setRemote(e.currentTarget.checked)}
            />{" "}
            Remote
          </label>
          <button
            type="button"
            class="btn btn-sm btn-primary"
            disabled={
              !label.trim() ||
              !country.trim() ||
              !indeedCountry.trim() ||
              distance < 5 ||
              distance > 250
            }
            onClick={() =>
              void addLocation({
                id:
                  label
                    .trim()
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, "_") || "location",
                label: label.trim(),
                search_label: searchLabel.trim() || label.trim(),
                country,
                indeed_country: indeedCountry,
                is_remote: remote,
                distance,
              })
            }
          >
            Use location
          </button>
        </div>
      )}
      {saving && <span role="status">Saving location…</span>}
      {error && (
        <p role="alert" class="text-red">
          {error}
        </p>
      )}
    </fieldset>
  );
}
