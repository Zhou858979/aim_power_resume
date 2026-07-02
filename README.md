# aim_power_resume
关于klipper的断电续打项目
## 下载与安装
```
cd ~
git clone https://github.com/Zhou858979/aim_power_resume.git
cd aim_power_resume
./install.sh
```
## 配置文件
```
[virtual_sdcard]
path: ~/printer_data/gcodes

[aim_power_resume]
detect_pin: ^PC4
is_shutdown: False
paused_recover_z: 2.0
layer_count: 0

power_off_gcode:
    M118 Power loss detected, saving state

start_gcode:
    M118 Power loss resume start
    M118 File: {PLR.print_stats.filename}
    M118 Pos X{PLR.POS_X} Y{PLR.POS_Y} Z{PLR.POS_Z} E{PLR.POS_E}

    {% if PLR.bed.target > 0 %}
    M140 S{PLR.bed.target}
    M190 S{PLR.bed.target}
    {% endif %}

    {% if PLR.extruder.target > 0 %}
    M104 S{PLR.extruder.target}
    M109 S{PLR.extruder.target}
    {% endif %}

    M106 S{PLR.fan_speed}
    M220 S{PLR.move_speed_percent}
    M221 S{PLR.extrude_speed_percent}
```