# Feature: Overall

## 1. Summary
This is for the overall progress of the project

## 2. Requirements
- [ ] Managing plugins on multiple devices
    - [X] Local
    - [ ] Start plugin when all other got loaded as well
    - [ ] Knowing remote plugins
    - [ ] Plugins
        - [X] Loading
        - [ ] Hot-swappable 
        - [ ] Kill/Reload plugin
- [ ] Communication between plugins
    - [ ] Remote
        - [ ] Networking
        - [ ] Error handling
    - [X] Local
        - [X] Call local + return

## 3. Inputs / Outputs
- **Input**: config.yml
- **Output**: based on plugins

## 4. Architecture & Flow
1. main loads PluginCore
2. PluginCore loads plugins based on config
3. main calls a method via execute
4. PluginCore creates a Request 
5. PluginCore looks for the plugin locally
6. Performs the action if requirements are met
7. Returns output (error or normal output) to main

## 5. Files and Functions
In /features

## 6. Edge Cases & Risks

## 7. Test Plan
- [ ] Test bidirectional communication between devices

## 8. Success Criteria
- Feature is working correctly
- It does not break existing functionality
