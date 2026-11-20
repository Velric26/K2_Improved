# /// script
# requires-python = ">=3.12"
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "K2 Calibration"
# description = "Run Auto Max Flow and Auto Pressure Advance on the Creality K2."
# version = "1.3.2"
# ///

import http.client
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit

import orca


COMPANION_PORT = 8766
MODES = {"max_flow": "Auto Max Flow", "pressure_advance": "Auto PA", "flow_then_pa": "Max Flow then PA"}


PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { padding: 24px; margin: 0; }
  main { max-width: 680px; margin: 0 auto; }
  h1 { font-size: 24px; margin: 0 0 6px; }
  h2 { font-size: 17px; margin: 20px 0 10px; }
  p { line-height: 1.5; }
  #printer { margin: 0 0 18px; }
  .fields { display: grid; gap: 12px; grid-template-columns: 2fr 1fr; }
  label { display: grid; gap: 5px; min-width: 0; }
  select { width: 100%; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 14px 0; }
  .actions button { padding: 7px 12px; }
  #status { min-height: 1.5em; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.5; }
  @media (max-width: 460px) { .fields { grid-template-columns: 1fr; } }
</style>
<main>
  <h1>Auto Calibrations</h1>
  <p id="printer">Connecting to the K2 companion…</p>
  <div class="fields">
    <label>Orca filament preset<select id="preset" aria-describedby="preset-note"></select></label>
    <label>CFS slot<select id="slot"></select></label>
  </div>
  <p id="preset-note" hidden></p>
  <div class="actions">
    <button type="button" id="flow">Auto Max Flow</button>
    <button type="button" id="pa">Auto PA</button>
    <button type="button" id="both">Max Flow then PA</button>
  </div>
  <div class="actions">
    <button type="button" id="refresh">Refresh presets and slots</button>
    <button type="button" id="cancel" disabled>Cancel</button>
  </div>
  <p id="status" role="status" aria-live="polite"></p>
  <section id="result" hidden>
    <h2>Result</h2>
    <pre id="report"></pre>
    <button type="button" id="apply" hidden>Apply to filament preset</button>
  </section>
</main>
<script>
  const el = id => document.getElementById(id);
  const send = (type, extra={}) => window.orca.postMessage({type, ...extra});
  let state = {profiles: [], slots: [], busy: true};
  const chosenPreset = () => state.profiles.find(p => p.name === el('preset').value);
  function updateSlots(previous) {
    const preset = chosenPreset();
    const slots = preset && !preset.issue ? state.slots.filter(s => s.material === preset.material) : [];
    el('preset-note').textContent = preset?.issue || '';
    el('preset-note').hidden = !preset?.issue;
    el('slot').replaceChildren();
    el('slot').add(new Option('Select a CFS slot', ''));
    for (const s of slots) {
      el('slot').add(new Option(s.slot + ' · ' + s.material + (s.loaded ? ' · Loaded' : ''), s.slot));
    }
    if (slots.some(s => s.slot === previous)) el('slot').value = previous;
    updateButtons();
  }
  function updateButtons() {
    const slot = state.slots.find(s => s.slot === el('slot').value);
    const preset = chosenPreset();
    const ready = !state.busy && state.printer && slot && preset && !preset.issue && slot.material === preset.material;
    const flow = ready && slot.max_flow && preset.temperature === (preset.material === 'PETG' ? 240 : 280);
    el('flow').disabled = !flow;
    el('pa').disabled = !ready || preset.adaptive;
    el('both').disabled = !flow || preset.adaptive;
    el('preset').disabled = state.busy || !state.profiles.length;
    el('slot').disabled = state.busy || !preset || Boolean(preset.issue);
    el('refresh').disabled = state.busy;
    el('cancel').disabled = !state.can_cancel;
    el('apply').disabled = state.busy;
  }
  el('preset').onchange = () => updateSlots('');
  el('slot').onchange = updateButtons;
  for (const [id, mode] of Object.entries({flow:'max_flow', pa:'pressure_advance', both:'flow_then_pa'})) {
    el(id).onclick = () => {
      send('start', {mode, preset:el('preset').value, slot:el('slot').value});
    };
  }
  for (const type of ['refresh','cancel','apply']) el(type).onclick = () => send(type);
  window.orca.onMessage(message => {
    const oldPreset = el('preset').value, oldSlot = el('slot').value;
    state = message;
    el('printer').textContent = state.printer ? 'Creality K2 · ' + state.printer : 'K2 companion unavailable';
    el('preset').replaceChildren();
    for (const p of state.profiles) {
      const temperature = p.temperature ? ' · ' + p.temperature + ' °C' : '';
      el('preset').add(new Option(p.name + temperature, p.name));
    }
    if (state.profiles.some(p => p.name === oldPreset)) el('preset').value = oldPreset;
    else if (state.default_preset) el('preset').value = state.default_preset;
    updateSlots(oldSlot);
    el('status').textContent = state.status || '';
    el('result').hidden = !state.report;
    el('report').textContent = state.report || '';
    el('apply').hidden = !state.can_apply;
    updateButtons();
  });
  updateButtons();
  send('refresh');
</script></html>"""


def companion(method, endpoint, payload=None):
    connection = http.client.HTTPConnection("127.0.0.1", COMPANION_PORT, timeout=3)
    body = json.dumps(payload) if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        connection.request(method, endpoint, body, headers)
        response = connection.getresponse()
        value = json.load(response)
        if not 200 <= response.status < 300:
            raise RuntimeError(value.get("message", f"K2 companion returned HTTP {response.status}."))
        return value
    finally:
        connection.close()


def scalar(value):
    return value[0] if isinstance(value, (list, tuple)) else value


def calibration_profile(preset, edited, assigned):
    if edited.name == preset.name and edited.is_dirty:
        raise ValueError(f"Save the changes to {preset.name} before calibrating.")

    def value(key):
        return scalar(preset.config_value(key))

    material = value("filament_type")
    temperature = int(value("nozzle_temperature"))
    diameter = float(value("filament_diameter"))
    current_flow = float(value("filament_max_volumetric_speed"))
    if material not in ("PETG", "ABS") or temperature not in ({"PETG": (230, 240), "ABS": (280,)}[material]):
        raise ValueError("Auto calibration supports PETG at 230–240 °C or ABS at 280 °C.")
    if not preset.is_compatible and not assigned:
        raise ValueError("This preset is for a different printer or nozzle. Select a compatible K2 preset.")
    if not all(math.isfinite(v) and v > 0 for v in (diameter, current_flow)):
        raise ValueError("Set a valid filament diameter and maximum volumetric speed.")
    adaptive = value("adaptive_pressure_advance")
    return {"name": preset.name, "file": preset.file, "material": material,
            "temperature": temperature, "diameter": diameter, "flow": current_flow,
            "pa": value("pressure_advance"), "enabled": value("enable_pressure_advance"),
            "adaptive": adaptive in (True, 1, "1", "true")}


def get_profile(bundle, name):
    preset = bundle.filaments.find_preset(name)
    if preset is None or preset.name != name or not preset.is_user() or not preset.file:
        raise ValueError("Select a saved user filament preset.")
    assigned = any(p is not None and p.name == name for p in bundle.current_filament_presets())
    return calibration_profile(preset, bundle.filaments.edited_preset(), assigned)


def list_profiles(bundle):
    collection = bundle.filaments
    edited = collection.edited_preset()
    assigned = [preset.name for preset in bundle.current_filament_presets() if preset is not None]
    profiles = []
    for index in range(collection.size()):
        preset = collection.preset(index)
        if not preset.is_user() or not preset.file:
            continue
        try:
            profile = calibration_profile(preset, edited, preset.name in assigned)
        except ValueError as error:
            profiles.append({"name": preset.name, "issue": str(error)})
        except (TypeError, IndexError, OverflowError):
            profiles.append({"name": preset.name, "issue": "Check the saved temperature, diameter and max flow settings."})
        else:
            profiles.append({key: profile[key] for key in ("name", "material", "temperature", "adaptive")})
    profiles.sort(key=lambda p: p["name"].casefold())
    ready = [p["name"] for p in profiles if not p.get("issue")]
    default = next((name for name in assigned if name in ready), ready[0] if ready else None)
    return profiles, default


def wall_flow(bundle, nozzle, wall):
    def config(key):
        return scalar(bundle.full_config_value(key))

    def width_value(value):
        if isinstance(value, str) and value.endswith("%"):
            return nozzle * float(value[:-1]) / 100
        if isinstance(value, dict):
            return float(value["value"]) * (nozzle / 100 if value["percent"] else 1)
        return float(value)

    width = width_value(config(f"{wall}_wall_line_width")) or width_value(config("line_width")) or 1.125 * nozzle
    layer, speed = float(config("layer_height")), float(config(f"{wall}_wall_speed"))
    area = layer * (width - layer * (1 - math.pi / 4))
    if not all(math.isfinite(v) for v in (width, layer, speed, area)) or area <= 0 or layer <= 0 or speed < 0:
        raise ValueError(f"Check {wall} wall width, speed and layer height in the process preset.")
    return area * speed


def selected_nozzle(bundle, printer):
    preset = bundle.current_printer_preset()
    address = str(preset.config_value("print_host") or "")
    host = urlsplit(address if "://" in address else "//" + address).hostname
    if host != printer:
        raise ValueError(f"Select the Orca printer preset that connects to {printer}.")
    return float(scalar(preset.config_value("nozzle_diameter")))


def request_for(mode, profile, slot, printer, nozzle, outer, inner):
    if mode not in MODES:
        raise ValueError("Choose a calibration.")
    if not math.isclose(nozzle, .4, abs_tol=1e-6):
        raise ValueError("This calibration requires the K2's 0.4 mm nozzle.")
    if slot["material"] != profile["material"]:
        raise ValueError("The CFS material needs to match the filament preset.")
    if mode != "max_flow" and profile["adaptive"]:
        raise ValueError("Choose a filament preset with fixed pressure advance.")
    if mode != "pressure_advance" and (not slot["max_flow"] or
            profile["temperature"] != (240 if profile["material"] == "PETG" else 280)):
        raise ValueError("Max Flow supports PETG at 240 °C and ABS at 280 °C in CFS unit 1.")
    payload = {"kind": mode, "printer": printer, "slot": slot["slot"], "material": profile["material"],
               "temperature_c": profile["temperature"], "nozzle_mm": .4,
               "filament_diameter_mm": profile["diameter"], "max_flow_mm3_s": profile["flow"]}
    if mode == "pressure_advance":
        max_flow = profile["flow"]
        lower, peak = sorted(min(flow or max_flow, max_flow) for flow in (outer, inner))
        limit = slot["pa_peak_limits_mm3_s"].get(str(profile["temperature"]))
        if limit is None or peak > limit:
            allowed = f"up to {limit:g} mm³/s" if limit is not None else "no flow"
            raise ValueError(f"The active wall settings request {peak:g} mm³/s. Auto PA supports {allowed} "
                             f"at {profile['temperature']} °C for {profile['material']} in CFS {slot['slot']}.")
        payload.update(reader="continuous", lower_flow_mm3_s=lower, max_flow_mm3_s=peak)
    elif mode == "flow_then_pa":
        payload.update(outer_wall_flow_mm3_s=outer, inner_wall_flow_mm3_s=inner)
    return payload


def describe_result(mode, result, request):
    lines, changes = [], {}
    flow = result.get("max_flow") if mode == "flow_then_pa" else result if mode == "max_flow" else None
    pa = result.get("pressure_advance") if mode == "flow_then_pa" else result if mode == "pressure_advance" else None
    if flow is not None:
        cutoff = flow.get("recommended_mm3_s")
        if cutoff is None:
            lines.append("Max Flow found no verified pair around the 1.0 current cutoff.")
        else:
            lines.extend([f"Max volumetric speed: {cutoff:g} mm³/s",
                          f"{flow['below_cutoff_mm3_s']:g} mm³/s: median |Iq| {flow['below_cutoff_median_abs_iq']:.5g} (below 1.0)",
                          f"{cutoff:g} mm³/s: median |Iq| {flow['boundary_median_abs_iq']:.5g} (at or above 1.0)"])
            changes["filament_max_volumetric_speed"] = f"{cutoff:g}"
        if flow.get("capture"):
            lines.append(f"Flow report: {flow['capture']}")
    if mode == "flow_then_pa" and pa is None:
        lines.append(result["pa_message"])
    if mode == "flow_then_pa" and result.get("pa_peak_mm3_s") is not None:
        peak, lower = result["pa_peak_mm3_s"], result["pa_lower_mm3_s"]
        lines.append(f"PA measurement targets: {lower:.4g} and {peak:.4g} mm³/s.")
    elif pa is not None:
        lines.append(f"PA measurement targets: {request['lower_flow_mm3_s']:.4g} and {request['max_flow_mm3_s']:.4g} mm³/s.")
    if pa is not None:
        candidate = pa.get("candidate")
        if candidate is None:
            lines.append("Auto PA found no eligible pressure advance value.")
        else:
            lines.append(f"Pressure advance: {candidate:.4f}")
            changes.update(pressure_advance=f"{candidate:.6g}", enable_pressure_advance="1")
            scores = pa.get("selected_scores")
            if scores and len(scores) == 4:
                lines.append(f"Lower flow scores: speedup {scores[0]:.4f}, slowdown {scores[1]:.4f}")
                lines.append(f"Higher flow scores: speedup {scores[2]:.4f}, slowdown {scores[3]:.4f}")
        if pa.get("report_file"):
            lines.append(f"PA report: {pa['report_file']}")
    return "\n".join(lines), changes


class AutoCalibrations(orca.script.ScriptPluginCapabilityBase):
    def __init__(self):
        super().__init__()
        self.state = {"profiles": [], "slots": [], "status": "", "busy": False}
        self.health = {}
        self.cancel = threading.Event()
        self.job_path = None
        self.apply_result = None
        self.window = None

    def get_name(self):
        return "Auto Calibrations"

    def execute(self):
        if self.window is None or not self.window.is_open():
            self.window = orca.host.ui.create_window(
                html=PAGE, title=self.get_name(), width=760, height=610,
                on_message=self.on_message, on_close=self.on_window_close,
            )
        return orca.ExecutionResult.success("Auto Calibrations opened.")

    def publish(self, **changes):
        self.state.update(changes)
        window = self.window
        if window is not None:
            window.post(dict(self.state))

    def launch(self, function, *args):
        try:
            threading.Thread(target=function, args=args, daemon=True).start()
        except (RuntimeError, OSError) as error:
            self.publish(busy=False, can_cancel=False, status=str(error))

    def refresh(self):
        profiles, default = list_profiles(orca.host.preset_bundle())
        self.publish(busy=True, profiles=profiles, default_preset=default, status="Reading CFS slots…")
        self.launch(self.read_slots, bool(profiles))

    def read_slots(self, has_profiles):
        try:
            health = companion("GET", "/health")
            if not health.get("combined_calibration"):
                raise RuntimeError("Restart the K2 companion with the combined-calibration update.")
            if not health.get("standalone_pa_uses_wall_flows"):
                raise RuntimeError("Restart the K2 companion with the wall-flow update.")
            slots = companion("GET", "/slots")["slots"]
            self.health = health
            status = "Select a filament preset and its matching CFS slot." if has_profiles else "Save a user filament preset in Orca first."
            self.publish(busy=False, printer=health["printer"], slots=slots, status=status)
        except (RuntimeError, OSError, ValueError, KeyError, http.client.HTTPException) as error:
            self.health = {}
            self.publish(busy=False, printer=None, slots=[], status=f"Could not read the K2. {error}")

    def start(self, mode, name, address):
        if not self.health:
            raise RuntimeError("Connect the K2 companion and refresh the slots.")
        slot = next((s for s in self.state["slots"] if s["slot"] == address), None)
        if slot is None:
            raise ValueError("Select a connected CFS slot.")
        bundle = orca.host.preset_bundle()
        profile = get_profile(bundle, name)
        nozzle = selected_nozzle(bundle, self.health["printer"])
        outer = wall_flow(bundle, nozzle, "outer") if mode != "max_flow" else 0.
        inner = wall_flow(bundle, nozzle, "inner") if mode != "max_flow" else 0.
        payload = request_for(mode, profile, slot, self.health["printer"], nozzle, outer, inner)
        text = (f"{MODES[mode]} · {profile['name']} · CFS {address}\n\n"
                f"Clear the build plate and purge chute and stay by the printer. "
                f"The nozzle will heat to {profile['temperature']} °C and the toolhead will move.")
        if mode == "flow_then_pa":
            text += ("\n\nAuto PA uses the verified Max Flow as its high target, subject to the printer's extrusion limits.")
            text += " Keep the purge chute and build area clear throughout both tests."
        elif mode == "pressure_advance":
            ceiling = profile["flow"]
            text += (f"\n\nPA wall-flow targets from the active process preset\n"
                     f"Outer wall {min(outer or ceiling, ceiling):.4g} mm³/s\n"
                     f"Inner wall {min(inner or ceiling, ceiling):.4g} mm³/s")
        if orca.host.ui.message(text, title=self.get_name(), buttons="yes_no", icon="question") != "yes":
            return
        self.cancel.clear()
        self.apply_result = None
        self.publish(busy=True, can_cancel=True, can_apply=False, report="", status="Starting calibration…")
        self.launch(self.run, payload, profile)

    def run(self, payload, profile):
        try:
            job = companion("POST", "/calibrations", payload)
            if not re.fullmatch("[a-f0-9]{32}", job["id"]):
                raise ValueError("The K2 companion returned an invalid calibration ID.")
            self.job_path = "/calibrations/" + job["id"]
            cancel_sent = False
            failed_polls = 0
            while job["state"] == "running":
                try:
                    if self.cancel.is_set() and not cancel_sent:
                        job = companion("DELETE", self.job_path)
                        cancel_sent = True
                    else:
                        job = companion("GET", self.job_path)
                    failed_polls = 0
                except (OSError, http.client.HTTPException):
                    failed_polls += 1
                    if failed_polls >= 3:
                        raise
                self.publish(can_cancel=not cancel_sent,
                             status="Canceling after the current printer operation…" if cancel_sent else job.get("message", "Measuring…"))
                if job["state"] == "running":
                    time.sleep(1)
            if job["state"] != "complete":
                self.publish(status=job.get("message", "Calibration ended."))
                return
            report, changes = describe_result(payload["kind"], job["result"], payload)
            if changes:
                self.apply_result = (profile, changes)
            self.publish(report=f"Filament preset: {profile['name']}\n{report}",
                         status="Calibration finished.", can_apply=bool(changes))
        except (RuntimeError, OSError, ValueError, KeyError, http.client.HTTPException) as error:
            if self.job_path:
                try:
                    companion("DELETE", self.job_path)
                except (RuntimeError, OSError, ValueError, http.client.HTTPException):
                    pass
            self.publish(status=f"Calibration could not finish. {error}")
        finally:
            self.job_path = None
            self.publish(busy=False, can_cancel=False)

    def apply(self):
        profile, changes = self.apply_result
        bundle = orca.host.preset_bundle()
        if get_profile(bundle, profile["name"]) != profile:
            raise ValueError("The tested filament preset changed. Restore the tested preset before applying this result.")
        slot = next((number for number, assigned in enumerate(bundle.current_filament_presets(), 1)
                     if assigned is not None and assigned.name == profile["name"]), None)
        if slot is None:
            raise ValueError(f"Assign {profile['name']} to a filament in Prepare first, then click Apply again.")
        self.publish(busy=True, status="Applying the result to the Orca filament editor…")
        self.launch(self.apply_changes, slot, profile, changes)

    def apply_changes(self, slot, profile, changes):
        try:
            transaction = orca.host.preset.filament.begin_update(slot)
            if transaction.target_name != profile["name"]:
                raise RuntimeError("The filament assigned in Orca changed.")
            for key, value in changes.items():
                transaction.set(key, value)
            transaction.commit()
            self.apply_result = None
            self.publish(status=f"Applied to {profile['name']}. Save the filament preset in Orca to keep it.",
                         can_apply=False)
        except (RuntimeError, ValueError) as error:
            self.publish(status=str(error))
        finally:
            self.publish(busy=False)

    def on_message(self, message):
        if not isinstance(message, dict):
            return
        action = message.get("type")
        if action == "cancel" and self.state.get("can_cancel"):
            self.cancel.set()
            self.publish(can_cancel=False, status="Canceling after the current printer operation…")
            return
        if self.state["busy"]:
            self.publish()
            return
        try:
            if action == "refresh":
                self.refresh()
            elif action == "start":
                self.start(message["mode"], message["preset"], message["slot"])
            elif action == "apply" and self.apply_result:
                self.apply()
        except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as error:
            self.publish(status=str(error))

    def on_window_close(self):
        self.window = None
        self.cancel.set()

    def on_unload(self):
        self.cancel.set()
        if self.job_path:
            try:
                companion("DELETE", self.job_path)
            except (RuntimeError, OSError, ValueError, http.client.HTTPException):
                pass


@orca.plugin
class K2CalibrationPlugin(orca.base):
    def register_capabilities(self):
        orca.request_permissions(network=[f"http://127.0.0.1:{COMPANION_PORT}"], threading=True)
        orca.register_capability(AutoCalibrations)
