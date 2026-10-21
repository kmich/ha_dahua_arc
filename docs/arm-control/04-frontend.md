# 04 — Frontend: what the user sees

The integration needs **no custom frontend code**: no custom card, no panel, no
JavaScript. Everything below is built from Home Assistant's own UI driven by
standard `alarm_control_panel` entities, config/options flow forms and
translation strings. "Building the frontend" for this project means getting
entity features, attributes, form schemas and strings exactly right, and
shipping documented dashboard YAML.

Entity IDs assume the ARC device is named `ARC3800H` with areas
`Ground floor` and `Upstairs`.

## 1. Options flow

### 1.1 Step `init` (existing step, one new checkbox)

```text
┌──────────────────────────────────────────────────────────────┐
│ Dahua ARC options                                            │
│ Adjust reconciliation, area matching and arm control.        │
│ Saving options reloads the integration.                      │
│                                                              │
│ Authoritative resync interval (seconds)        [ 300      ]  │
│ [ ] Smartly assign devices and sensors to areas              │
│ [ ] Enable PIRCam / LowRateWPAN research tools               │
│ [ ] Allow Home Assistant to arm and disarm the ARC   ← NEW   │
│     Off by default. When on, Home Assistant can arm and      │
│     disarm the alarm from dashboards, automations and        │
│     voice. You can require a code on the next screen.        │
│                                                              │
│                                              [ Submit ]      │
└──────────────────────────────────────────────────────────────┘
```

### 1.2 Step `arm_control` (new)

```text
┌──────────────────────────────────────────────────────────────┐
│ Arm and disarm control                                       │
│                                                              │
│ Home Assistant will send arm and disarm commands to the ARC  │
│ and confirm each one from the ARC's own events. Anything     │
│ that can run Home Assistant actions can use this, including  │
│ automations and voice assistants. Set a code to restrict it. │
│                                                              │
│ Code                                         [ ••••       ]  │
│   4–8 digits. Leave empty to keep the current code.          │
│ [ ] Remove the code                                          │
│ [x] Require the code to disarm                               │
│ [ ] Require the code to arm                                  │
│ Arm modes offered                                            │
│   [x] Home   [x] Away                                        │
│ [ ] I understand that Home Assistant can now disarm my alarm │
│                                                              │
│                                              [ Submit ]      │
└──────────────────────────────────────────────────────────────┘
```

Behaviour:

- The code field is never pre-filled, and the stored hash never reaches the
  browser (the same rule as the reconfigure password field).
- The acknowledgement box is required only the first time arm control is turned
  on. Error: *"Confirm that you understand Home Assistant will be able to disarm
  the alarm."*
- A code that is not 4–8 digits gives the error *"The code must be 4 to 8
  digits."*
- After Night (`p2`) is verified, the modes list gains `[ ] Night (partial 2)`.

### 1.3 Repair card when no code is set

```text
⚠ Dahua ARC can disarm your alarm without a code
  Arm control is on and no code is set, so any dashboard user,
  automation or voice assistant can disarm the ARC. Set a code
  under Settings → Devices & services → Dahua ARC → Configure,
  or submit to keep using arm control without a code.
                                                  [ Submit ]
```

## 2. Device page

Under **Settings → Devices & services → Dahua ARC → ARC3800H** the panels
appear in the **Controls** section above the existing sensors:

```text
Controls
  🛡  ARC3800H                    Disarmed        ← system panel
  🛡  Ground floor                Disarmed        ← area panel (≥2 areas only)
  🛡  Upstairs                    Armed home
Sensors
  Arm state                       Mixed
  Ground floor arm state          Disarmed
  …
```

## 3. More-info dialog (built-in)

### 3.1 Disarmed, no code

```text
┌──────────────────────────────────────┐
│  ARC3800H                        ⋮ ✕ │
│                                      │
│            ( 🛡 )                    │
│           Disarmed                   │
│                                      │
│   [  Home  ]  [  Away  ]             │
│                                      │
│  ⚠ Open now: Kitchen Window          │  ← from `open_zones` attribute
│                                      │     (shown in Attributes; see 4.3
│  Attributes ▾                        │      for a visible dashboard warning)
└──────────────────────────────────────┘
```

The mode buttons come from `supported_features`, so only Home and Away appear.

### 3.2 With a code (`code_format: number`)

Pressing a mode opens HA's built-in keypad:

```text
┌──────────────────────────────┐
│  Enter code to disarm        │
│        [ ••••     ]          │
│   1   2   3                  │
│   4   5   6                  │
│   7   8   9                  │
│   ✕   0   ✓                  │
└──────────────────────────────┘
```

### 3.3 While a command is being confirmed

For up to about 10 s the state reads **Arming** or **Disarming** (from the
entity's `_pending`). HA shows the transitional icon. The buttons stay visible;
a second press during this time is rejected with the "busy" toast.

### 3.4 Triggered

```text
            ( 🚨 )
           Triggered
   [ Disarm ]
```

Attributes show `alarm_zones` (via the existing Alarm binary sensors) and
`last_command`.

### 3.5 Failure toasts

HA shows a raised `HomeAssistantError` as a toast at the bottom of the screen.
The exact strings are in 03 §4. The ones users will see most:

```text
✖ The ARC refused to arm Ground floor: Kitchen Window, Office Window open.
  Close them, or use Force arm.

✖ The code is incorrect.

✖ The ARC accepted the command but did not confirm the change within
  10 seconds. Check the panel before trying again.

✖ Arm and disarm are unavailable while the ARC is offline.
```

## 4. Recommended dashboard: "Security" view

Ship this YAML in the README (phase 4). It uses only core cards.

### 4.1 Layout (sections view)

```text
┌─────────────────────────────┬──────────────────────────────┐
│ ALARM                       │ AREAS                        │
│ ┌─────────────────────────┐ │ ┌──────────────────────────┐ │
│ │  alarm-panel card       │ │ │ Ground floor   Disarmed  │ │
│ │   Disarmed              │ │ │ [Disarm][Home][Away]     │ │ tile +
│ │   [Arm home][Arm away]  │ │ ├──────────────────────────┤ │ alarm-modes
│ │   keypad (if code)      │ │ │ Upstairs     Armed home  │ │ feature
│ └─────────────────────────┘ │ │ [Disarm][Home][Away]     │ │
│ ┌─────────────────────────┐ │ └──────────────────────────┘ │
│ │ ⚠ Not ready to arm      │ │ ┌──────────────────────────┐ │
│ │ Ground floor:           │ │ │ Alarm            Off     │ │
│ │  • Kitchen Window       │ │ │ Ground floor alarm Off   │ │ entities
│ └─────────────────────────┘ │ │ Last arming failure 2h   │ │
│  (markdown card, 4.3)       │ └──────────────────────────┘ │
├─────────────────────────────┴──────────────────────────────┤
│ HISTORY  logbook card: panels + arm-state sensors, 24 h    │
└────────────────────────────────────────────────────────────┘
```

### 4.2 YAML

```yaml
title: Security
views:
  - title: Security
    path: security
    icon: mdi:shield-home
    type: sections
    max_columns: 2
    sections:
      - type: grid
        cards:
          - type: heading
            heading: Alarm
          - type: alarm-panel
            entity: alarm_control_panel.arc3800h
            name: House
            states:
              - arm_home
              - arm_away
          - type: markdown
            content: >-
              {% set z = state_attr('alarm_control_panel.arc3800h', 'open_zones') or [] %}
              {% if z %}
              **⚠ Not ready to arm**

              {% for zone in z %}- {{ zone }}
              {% endfor %}
              {% else %}
              ✅ All zones closed
              {% endif %}
      - type: grid
        cards:
          - type: heading
            heading: Areas
          - type: tile
            entity: alarm_control_panel.arc3800h_ground_floor
            features:
              - type: alarm-modes
                modes: [disarmed, armed_home, armed_away]
          - type: tile
            entity: alarm_control_panel.arc3800h_upstairs
            features:
              - type: alarm-modes
                modes: [disarmed, armed_home, armed_away]
          - type: entities
            entities:
              - binary_sensor.arc3800h_alarm
              - binary_sensor.arc3800h_ground_floor_alarm
              - binary_sensor.arc3800h_upstairs_alarm
              - sensor.arc3800h_last_arming_failure
      - type: grid
        column_span: 2
        cards:
          - type: logbook
            target:
              entity_id:
                - alarm_control_panel.arc3800h
                - alarm_control_panel.arc3800h_ground_floor
                - alarm_control_panel.arc3800h_upstairs
            hours_to_show: 24
```

### 4.3 The "Not ready to arm" card

This is the most useful piece of UX the integration can offer: the user sees
which doors and windows are open **before** pressing Arm, instead of after a
refusal. It depends on the live `open_zones` attribute (03 §2.6), which must be
a flat list of zone names to keep the template trivial. The area panels carry
the same attribute for per-area cards.

### 4.4 Single-area systems

Only `alarm_control_panel.arc3800h` exists. Drop the "Areas" tiles; keep the
alarm-panel card, the markdown card and the entities card.

## 5. Automations (README examples, phase 4)

### 5.1 Arm Away when everyone leaves, and say so if it fails

```yaml
alias: Arm when everyone leaves
triggers:
  - trigger: state
    entity_id: zone.home
    to: "0"
    for: "00:05:00"
actions:
  - action: alarm_control_panel.alarm_arm_away
    target:
      entity_id: alarm_control_panel.arc3800h
    continue_on_error: true
  - delay: "00:00:15"
  - if:
      - condition: not
        conditions:
          - condition: state
            entity_id: alarm_control_panel.arc3800h
            state: armed_away
    then:
      - action: notify.mobile_app_your_phone
        data:
          title: "Alarm NOT armed"
          message: >
            Open: {{ state_attr('alarm_control_panel.arc3800h', 'open_zones') | join(', ') or 'unknown reason' }}
```

`continue_on_error` matters: a refused arm raises an error, which would
otherwise stop the automation before the notification.

### 5.2 Disarm from an actionable notification (only with a code policy you accept)

Not shipped as an example. Disarming from a phone notification bypasses the HA
code. The README should say so rather than give the YAML.

## 6. Copy review checklist (for the implementer)

- Use "arm" and "disarm" (lower case) in sentences; "Home" and "Away" as mode
  names, matching the existing sensor states "Armed home" and "Armed away".
- Name zones and areas exactly as the ARC reports them; never use raw indexes in
  user-facing text.
- Every error says what happened and what to do next.
- Do not call the HA code a "PIN" anywhere. The ARC has its own user PINs and
  the two must not be confused.
- The `config.step.user.description` text ("It never arms, disarms or triggers
  outputs") must become: *"By default the integration only reads from the hub.
  Arm and disarm control can be turned on later in the options."*
