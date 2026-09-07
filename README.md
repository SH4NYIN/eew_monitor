# EEW Monitor
> This software was built assisted with gpt-5.6-sol / gpt-6-astra using Codex.

This is an [EEW](https://en.wikipedia.org/wiki/Earthquake_Early_Warning_(Japan)) (Earthquake Early Warning) software that pops warning info on your desktop (Windows) once an EEW message announces.

## Functions

- Receives EEW updates with WebSocket
  - Currently from [JMA](https://www.data.jma.go.jp/multi/quake/index.html) (Japan Meteorological Agency, 気象庁) only (credit to [Wolfx API](https://wolfx.jp/apidoc_en)).
  - Planning to support: [CENC](https://data.earthquake.cn/) (China Earthquake Networks Center, 国家地震科学数据中心) and more.
- Once received alert updates
  - Record files in json format, same file will be updated if the alert has been updated;
  - Short info messages printed in the terminal;
  - Pop-up window at the corner on Windows system.
- Compatibility
  - Expected to work both on Windows and Linux 


## Requirements

``` bash
python -m pip install -r requirements.txt
```

- Python 3.9+
- packages
  - `websockets==16.1.1`
  - `win11toast==0.36.3; sys_platform == "win32"`
  - `textual==8.2.8`

## To Run

- TUI (by default)

  ``` bash
  python eew.py 
  ```

  Major controls are shown on the TUI.

  `Ctrl + C` / `Q` only close the TUI window and does not stop the background receiving service, please use `python eew_service.py stop` to actually stop the program.

- Background receiver (service): double-click `eew_background.pyw` on Windows, or with commands: 

  ``` bash
  python eew_service.py start   
  python eew_service.py status  
  python eew_service.py retry
  python eew_service.py stop
  ```

## Misc

### Color 

Colors reflect the maximum intensity (Shindo, based on JMA's system) and I referred [JQuake](https://jquake.net/)'s color scheme. The table here shows the default color config. It cannot be changed for now.

| Intensity      | Color                                       |
| -------------- | ------------------------------------------- |
| 0–3            | Light gray (`#a3a8b0`)                      |
| 4              | Yellow                                      |
| 5 lower (`5-`) | Orange                                      |
| 5 upper (`5+`) | Orange-red                                  |
| 6 lower (`6-`) | Red                                         |
| 6 upper (`6+`) | Pink                                        |
| 7              | Purple                                      |
| Unknown        | Neutral gray                                |

### Threshold

Earthquake with maximum intensity `>= 4` or magnitude `>= 5.0` would be alerted with a desktop notification, regardless final or not.