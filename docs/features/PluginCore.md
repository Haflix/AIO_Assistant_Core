# Feature: PluginCore

## 1. Summary
Main class that loads and manages plugins and requests

## 2. Requirements
- [ ] Hot-Swappable plugins
    - [X] Load plugins
    - [ ] Hot swapping etc
- [X] Loads config on startup
- [ ] Handle Requests
    - [ ] Find compatible plugin (local & remote)
    - [X] Local execution
        - [X] Execute
        - [X] Return output
    - [ ] Remote execution 
        - [ ] MQTT?
- [X] Support both synchronous and asynchronous 

## 3. Inputs / Outputs
- **Input**: <How this feature is triggered or used>
- **Output**: <What it returns, does, or changes>

## 4. Architecture & Flow
<Diagram, flowchart, or step-by-step logic of how the feature works>

## 5. Files and Functions
- `filename.py`
  - `function_or_class_name()` – Description

## 6. Edge Cases & Risks
- List of weird cases, errors, or potential problems

## 7. Test Plan
- [ ] Test this case
- [ ] Handle that condition

## 8. Success Criteria
- Feature is working correctly
- It does not break existing functionality
- Optional: measurable improvement or benchmark
