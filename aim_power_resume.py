"""Klipper断电续打模块。

模块职责：
1. 监听MCU断电检测引脚；
2. 在断电窗口内保存虚拟SD卡打印现场；
3. 上电后通过G-code命令或Web API恢复打印。
"""

# Resuming printing after a power outage
#
# Copyright (C) 2025.11 SiYuan_ZHU <1137811735@qq.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import io
import json
import logging
import os


INFO_FILE = ".aim_power_loss_recover.json"


class AIMPowerLossResume:
    """断电续打控制器。

    该类按Klipper extras模块方式加载，依赖virtual_sdcard、gcode_move、
    print_stats、heaters等Klipper内部对象完成现场保存和恢复。
    """

    def __init__(self, config):
        """读取配置并注册Klipper事件、G-code命令和Web API端点。"""
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()

        self.printer.register_event_handler("klippy:shutdown", self.handle_shutdown)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        detect_pin = config.get("detect_pin")
        if detect_pin.startswith("host:"):
            raise self.printer.config_error(
                "aim_power_resume only supports MCU pins for detect_pin."
            )

        # detect_pin仅支持MCU引脚；state == 0时认为发生断电。
        self.buttons = self.printer.load_object(config, "buttons")
        self.buttons.register_buttons([detect_pin], self._power_button_handler)

        self.is_shutdown = config.getboolean("is_shutdown", True)
        self.paused_recover_z = config.getfloat("paused_recover_z", 0.0)
        self.layer_count = config.getint("layer_count", 0)

        gcode_macro = self.printer.load_object(config, "gcode_macro")
        self.start_gcode = gcode_macro.load_template(config, "start_gcode", "")
        self.layer_change_gcode = gcode_macro.load_template(
            config, "layer_change_gcode", ""
        )
        self.power_off_gcode = gcode_macro.load_template(
            config, "power_off_gcode", ""
        )

        self.gcode_move = self.printer.load_object(config, "gcode_move")
        self.gcode = self.printer.lookup_object("gcode")
        self.virtual_sdcard = self.printer.lookup_object("virtual_sdcard", None)
        self.exclude_objects = self.printer.lookup_object("exclude_object", None)
        self.webhooks = self.printer.lookup_object("webhooks")
        self.print_stats = self.printer.load_object(config, "print_stats")

        self.power_loss_info = None
        self.power_loss_triggered = False
        self.sdcard_dirname = f"/home/orangepi/printer_data/{INFO_FILE}"

        # 用户侧入口：GO_WORK用于恢复，CLEAR_POWER_LOSS_RESUME用于清除现场。
        self.gcode.register_command(
            "GO_WORK",
            self.cmd_START_POWER_LOSS_RESUME,
            desc=self.cmd_START_POWER_LOSS_RESUME_help,
        )
        self.gcode.register_command(
            "CLEAR_POWER_LOSS_RESUME",
            self.cmd_CLEAR_POWER_LOSS_RESUME,
            desc=self.cmd_CLEAR_POWER_LOSS_RESUME_help,
        )

        # 前端入口：供Mainsail/Fluidd或自定义页面调用。
        self.webhooks.register_endpoint(
            "aim_power_resume/start_print",
            self._handle_start_power_loss_resume,
        )
        self.webhooks.register_endpoint(
            "aim_power_resume/clear_info",
            self._handle_clear_power_loss_resume_info,
        )
        self.webhooks.register_endpoint(
            "aim_power_resume/get_info",
            self._handle_get_power_loss_resume_info,
        )

    def handle_shutdown(self):
        """Klipper关闭时保存打印现场。"""
        logging.info("power_loss_resume_handle_shutdown")
        self._save_printing_info()

    def _handle_ready(self):
        """Klipper ready后初始化运行期对象并读取续打信息。"""
        logging.info("power_loss_resume_handle_ready")

        self.virtual_sdcard = self.printer.lookup_object("virtual_sdcard", None)
        self.exclude_objects = self.printer.lookup_object("exclude_object", None)
        if self.virtual_sdcard is None:
            raise self.printer.config_error(
                "virtual_sdcard not found. Cannot start power loss resume."
            )

        # 续打信息放在gcodes目录的上一级，通常是~/printer_data。
        if hasattr(self.virtual_sdcard, "sdcard_dirname"):
            self.sdcard_dirname = os.path.join(
                os.path.dirname(self.virtual_sdcard.sdcard_dirname), INFO_FILE
            )
        if self.sdcard_dirname is None:
            raise self.printer.config_error(
                "sdcard_dirname not found. Cannot start power loss resume."
            )

        pheaters = self.printer.lookup_object("heaters")
        try:
            self.heater_bed = pheaters.lookup_heater("heater_bed")
        except self.printer.config_error:
            logging.info("heater_bed not found; bed temperature will be saved as 0")
            self.heater_bed = None
        self.extruder = pheaters.lookup_heater("extruder")

        self._read_power_loss_info()

    def _get_nested_value(self, data, keys):
        """安全读取嵌套字典字段，字段不存在时返回None。"""
        for key in keys:
            if not isinstance(data, dict) or key not in data:
                return None
            data = data[key]
        return data

    def _get_power_loss_info_error(self):
        """校验续打信息完整性。

        返回None表示可以恢复；返回字符串表示不可恢复的具体原因。
        """
        info = self.power_loss_info
        if not isinstance(info, dict):
            return "没有找到续打信息"
        if not info.get("aim_power_resume", False):
            return "没有需要续打的"

        required_paths = [
            ("file_path",),
            ("progress",),
            ("file_size",),
            ("file_position",),
            ("gcode_move",),
            ("gcode_move", "gcode_position"),
            ("gcode_move", "speed"),
            ("gcode_move", "absolute_extrude"),
            ("gcode_move", "absolute_coordinates"),
            ("print_stats",),
            ("print_stats", "filename"),
            ("print_stats", "total_duration"),
            ("print_stats", "state"),
            ("print_stats", "info"),
            ("extruder",),
            ("extruder", "temp"),
            ("extruder", "target"),
            ("bed",),
            ("bed", "temp"),
            ("bed", "target"),
            ("current_object",),
            ("fan_speed",),
            ("move_speed_percent",),
            ("extrude_speed_percent",),
        ]
        non_empty_paths = [
            ("file_path",),
            ("print_stats", "filename"),
        ]

        # 字段存在性和关键字段非空要分开检查，部分字段允许空字符串。
        for path in required_paths:
            value = self._get_nested_value(info, path)
            if value is None:
                return "续打信息不完整，缺少字段: %s" % ".".join(path)

        for path in non_empty_paths:
            value = self._get_nested_value(info, path)
            if value == "":
                return "续打信息不完整，字段为空: %s" % ".".join(path)

        gcode_position = info["gcode_move"]["gcode_position"]
        if not isinstance(gcode_position, list) or len(gcode_position) < 4:
            return "续打信息不完整，gcode_move.gcode_position无效"

        return None

    def _has_power_loss_resume(self):
        """返回当前是否有可用的断电续打信息。"""
        return self._get_power_loss_info_error() is None

    def get_status(self, eventtime):
        """Klipper状态接口，供前端查询是否可续打。"""
        return {"aim_power_resume": self._has_power_loss_resume()}

    cmd_CLEAR_POWER_LOSS_RESUME_help = "Clear power loss resume info"

    def cmd_CLEAR_POWER_LOSS_RESUME(self, gcmd):
        """G-code命令：清除断电续打现场。"""
        self._clear_power_loss_info()
        gcmd.respond_raw("Power loss resume info cleared")

    cmd_START_POWER_LOSS_RESUME_help = "Start power loss resume print"

    def cmd_START_POWER_LOSS_RESUME(self, gcmd):
        """G-code命令：启动断电续打流程。"""
        if self._is_sdcard_busy():
            gcmd.respond_raw("Printing in progress. Unable to operate.")
            return
        if self.power_loss_info is None:
            gcmd.respond_raw("No power loss info found.")
            return

        logging.info("start power loss resume print")
        self.reactor.register_async_callback(
            lambda eventtime: self._handle_power_loss_resume()
        )
        gcmd.respond_raw("Start power loss resume")

    def _handle_start_power_loss_resume(self, web_request):
        """Web API：启动断电续打流程。"""
        if self._is_sdcard_busy():
            web_request.send({"msg": "Printing in progress. Unable to operate."})
            return
        if self.power_loss_info is None:
            web_request.send({"msg": "No power loss info found."})
            return

        self.reactor.register_async_callback(
            lambda eventtime: self._handle_power_loss_resume()
        )
        web_request.send({"msg": "Start power loss resume"})

    def _handle_clear_power_loss_resume_info(self, web_request):
        """Web API：清除断电续打现场。"""
        self._clear_power_loss_info()
        web_request.send({"msg": "Clear power loss resume info"})

    def _handle_get_power_loss_resume_info(self, web_request):
        """Web API：返回当前续打状态和基础打印信息。"""
        aim_power_resume = self._has_power_loss_resume()
        ret = {
            "aim_power_resume": aim_power_resume,
            "file_path": self.power_loss_info["file_path"]
            if aim_power_resume
            else None,
            "progress": self.power_loss_info["progress"] if aim_power_resume else 0,
            "filename": self.power_loss_info["print_stats"]["filename"]
            if aim_power_resume
            else None,
        }
        web_request.send({"power_loss_info": ret})

    def _is_sdcard_busy(self):
        """判断虚拟SD卡是否已有打印任务正在运行。"""
        return self.virtual_sdcard.work_timer is not None

    def _clear_power_loss_info(self):
        """清除续打文件并复位断电触发锁。"""
        self._save_printing_info(False)
        self.power_loss_triggered = False

    def _read_power_loss_info(self):
        """从JSON文件读取上次断电保存的打印现场。"""
        if not os.path.exists(self.sdcard_dirname):
            self.power_loss_info = None
            return

        try:
            with open(self.sdcard_dirname, "r") as file:
                self.power_loss_info = json.load(file)
        except (OSError, ValueError):
            logging.exception(
                "Failed to read power loss info: %s", self.sdcard_dirname
            )
            self.power_loss_info = None

    def _write_power_loss_info(self):
        """用临时文件和原子替换写入，降低断电时写坏JSON的概率。"""
        info_dir = os.path.dirname(self.sdcard_dirname)
        tmp_path = self.sdcard_dirname + ".tmp"

        if info_dir and not os.path.exists(info_dir):
            os.makedirs(info_dir)

        with open(tmp_path, "w") as file:
            json.dump(self.power_loss_info, file)
            file.flush()
            os.fsync(file.fileno())

        os.replace(tmp_path, self.sdcard_dirname)

        if not info_dir:
            return

        try:
            dir_fd = os.open(info_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            logging.exception(
                "Failed to fsync power loss info directory: %s", info_dir
            )

    def _save_printing_info(self, is_power_loss=True):
        """保存或清除断电续打信息。"""
        print_stats = self.printer.lookup_object("print_stats", None)
        if is_power_loss and print_stats is not None and print_stats.state == "printing":
            self.power_loss_info = self._collect_printing_info()
        else:
            self.power_loss_info = {"aim_power_resume": False}

        self._write_power_loss_info()
        logging.info("power_loss_resume_info saved")
        logging.info(json.dumps(self.power_loss_info, indent=4))

    def _collect_printing_info(self):
        """采集恢复打印所需的最小现场信息。"""
        eventtime = self.reactor.monotonic()

        bed_temp, bed_target = self._get_bed_temp(eventtime)
        e_temp, e_target = self.extruder.get_temp(eventtime)
        bed_target = bed_temp if bed_temp > 0 and bed_target == 0 else bed_target
        e_target = e_temp if e_temp > 0 and e_target == 0 else e_target

        gcodestatus = self.gcode_move.get_status()
        printstats = self.print_stats.get_status(eventtime)
        if (
            printstats["filename"] == ""
            and self.power_loss_info is not None
            and "print_stats" in self.power_loss_info
        ):
            # 打印刚启动就断电时，print_stats可能短暂没有文件名。
            printstats = self.power_loss_info["print_stats"]

        fan = self.printer.lookup_object("fan", None)
        fan_speed = 255
        if fan is not None:
            fan_speed = int(fan.get_status(eventtime)["speed"] * 255)

        current_object = ""
        if self.exclude_objects is not None:
            current_object = self.exclude_objects.current_object

        return {
            "aim_power_resume": True,
            "file_path": self.virtual_sdcard.file_path(),
            "progress": self.virtual_sdcard.progress(),
            "is_active": self.virtual_sdcard.is_active(),
            "file_size": self.virtual_sdcard.file_size,
            "file_position": self.virtual_sdcard.file_position,
            "next_file_position": self.virtual_sdcard.next_file_position,
            "gcode_move": gcodestatus,
            "print_stats": printstats,
            "extruder": {"temp": e_temp, "target": e_target},
            "bed": {"temp": bed_temp, "target": bed_target},
            "current_object": current_object,
            "fan_speed": fan_speed,
            "move_speed_percent": gcodestatus["speed_factor"] * 100,
            "extrude_speed_percent": gcodestatus["extrude_factor"] * 100,
        }

    def _get_bed_temp(self, eventtime):
        """读取热床温度；无热床机器返回0。"""
        if self.heater_bed is None:
            return 0, 0
        return self.heater_bed.get_temp(eventtime)

    def _shutdown(self):
        """执行断电后的自定义G-code，并按配置调用系统关机。"""
        try:
            self.gcode.run_script(self.power_off_gcode.render())
        except Exception:
            logging.exception("Script running error")

        try:
            if self.is_shutdown:
                self.webhooks.call_remote_method("shutdown_machine")
        except self.printer.command_error:
            logging.exception("Remote Call Error")

    def _power_button_handler(self, eventtime, state):
        """断电检测引脚回调。

        Klipper buttons模块传入state，低电平(state == 0)表示断电触发。
        """
        if state != 0:
            return
        if self.power_loss_triggered:
            logging.info("Power loss event ignored because it is already handled")
            return

        self.power_loss_triggered = True
        logging.info("power loss detected")
        self._save_printing_info()
        self._shutdown()

    def run_layer_change_gcode(self):
        """恢复打印后可选的层变化G-code入口。"""
        if self.layer_change_gcode is None or self.layer_change_gcode == "":
            return

        logging.info("Layer Change Gcode")
        try:
            self.gcode.run_script(
                self.layer_change_gcode.render(
                    context=self.layer_change_gcode_context
                )
            )
        except Exception:
            logging.exception("Script running error")

    def _handle_power_loss_resume(self):
        """断电续打主流程。"""
        power_loss_info_error = self._get_power_loss_info_error()
        if power_loss_info_error is not None:
            self.gcode._respond_error(power_loss_info_error)
            return

        self.gcode.respond_raw("正在恢复打印")
        self.virtual_sdcard._reset_file()

        filename = self._get_resume_filename()
        if filename is None:
            self.gcode._respond_error("没有找到续打文件")
            return

        self._restore_print_stats(filename)

        # 先恢复Klipper内部坐标认知，再执行用户配置的恢复G-code。
        x, y, z, e = self.power_loss_info["gcode_move"]["gcode_position"][:4]
        self._restore_toolhead_position(x, y, z)

        plr = self._build_template_context(x, y, z, e)
        self._run_resume_gcode(plr)

        # 重新打开原G-code文件，并把虚拟SD卡读指针放回断点附近。
        if not self._open_resume_file():
            return

        self.virtual_sdcard.is_pwr_loss_resume = True
        self.virtual_sdcard.layer_change_count = 0
        self.virtual_sdcard.is_resume_speed = True
        self.virtual_sdcard.do_resume()

        self._clear_power_loss_info()
        logging.info("开始打印")

    def _get_resume_filename(self):
        """获取恢复时展示和统计使用的文件名。"""
        filename = self.power_loss_info["print_stats"]["filename"]
        if filename is None or filename == "":
            filename = os.path.basename(self.power_loss_info["file_path"])
        if filename is None or filename == "":
            return None
        return filename

    def _restore_print_stats(self, filename):
        """恢复print_stats，保证前端状态和统计信息连续。"""
        print_stats = self.power_loss_info["print_stats"]
        print_info = print_stats.get("info", {})
        self.print_stats.filename = print_stats["filename"]
        self.print_stats.total_duration = print_stats["total_duration"]
        self.print_stats.state = print_stats["state"]
        self.print_stats.error_message = print_stats.get("message", "")
        self.print_stats.info_total_layer = print_info.get("total_layer", 0)
        self.print_stats.info_current_layer = print_info.get("current_layer", 0)
        self.print_stats.set_current_file(filename)
        self.print_stats.note_start()

    def _restore_toolhead_position(self, x, y, z):
        """恢复Klipper内部工具头坐标。"""
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.get_last_move_time()
        curpos = toolhead.get_position()
        toolhead.set_position([x, y, z, curpos[3]], homing_axes="xyz")

    def _build_template_context(self, x, y, z, e):
        """构造传给start_gcode和layer_change_gcode的PLR上下文。"""
        return {
            "POS_X": x,
            "POS_Y": y,
            "POS_Z": z,
            "POS_E": e,
            "print_stats": self.power_loss_info["print_stats"],
            "gcode_move": self.power_loss_info["gcode_move"],
            "extruder": self.power_loss_info["extruder"],
            "bed": self.power_loss_info["bed"],
            "current_object": self.power_loss_info["current_object"],
            "fan_speed": self.power_loss_info["fan_speed"],
            "move_speed_percent": self.power_loss_info["move_speed_percent"],
            "extrude_speed_percent": self.power_loss_info["extrude_speed_percent"],
        }

    def _run_resume_gcode(self, plr):
        """执行用户配置的恢复G-code和内置位置恢复G-code。"""
        context = self.start_gcode.create_template_context()
        context.update({"PLR": plr})

        if self.layer_change_gcode is not None and self.layer_change_gcode != "":
            self.layer_change_gcode_context = (
                self.layer_change_gcode.create_template_context()
            )
            self.layer_change_gcode_context.update({"PLR": plr})

        try:
            self.gcode.run_script(self.start_gcode.render(context=context))
            for line in self._build_position_restore_gcode(plr):
                self.gcode.run_script(line)
        except Exception:
            logging.exception("Script running error")

    def _build_position_restore_gcode(self, plr):
        """生成恢复坐标模式、挤出模式和E轴位置的G-code。"""
        gcode_move = self.power_loss_info["gcode_move"]
        lines = [
            f"G1 F{gcode_move['speed']}",
            "G90",
            f"G1 X{plr['POS_X']} Y{plr['POS_Y']} F6000",
        ]
        if self.paused_recover_z > 0.0:
            lines.extend(["G91", f"G1 Z{self.paused_recover_z}"])
        lines.extend(["G90", f"G1 Z{plr['POS_Z']} F300"])

        lines.append("M82" if gcode_move["absolute_extrude"] else "M83")
        lines.append(f"G92 E{plr['POS_E']}")
        lines.append("G90" if gcode_move["absolute_coordinates"] else "G91")
        return lines

    def _open_resume_file(self):
        """重新打开原G-code文件，并设置virtual_sdcard恢复打印所需状态。"""
        try:
            current_position = self._get_resume_file_position()
            f = io.open(
                self.power_loss_info["file_path"], "r", newline="", errors="ignore"
            )
            self.virtual_sdcard._gcode_file_path = self.power_loss_info["file_path"]

            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            current_position = self._clamp_file_position(current_position, fsize)
            current_position = self._find_line_start(f, current_position)

            self.virtual_sdcard.file_position = current_position
            self.virtual_sdcard.current_file = f
            self.virtual_sdcard.file_size = fsize
            self.virtual_sdcard.current_file.seek(self.virtual_sdcard.file_position)
            return True
        except Exception:
            logging.exception("virtual_sdcard file open")
            self.gcode._respond_error("Unable to open file")
            return False

    def _get_resume_file_position(self):
        """读取保存的G-code文件偏移。"""
        return int(self.power_loss_info["file_position"])

    def _clamp_file_position(self, position, file_size):
        """限制文件偏移在有效范围内。"""
        if position > file_size:
            logging.warning(
                "Saved file position %s is past file size %s; using EOF",
                position,
                file_size,
            )
            return file_size
        if position < 0:
            logging.warning(
                "Saved file position %s is invalid; using file start", position
            )
            return 0
        return position

    def _find_line_start(self, file_obj, position):
        """从保存偏移向前回退到完整G-code行的起点。"""
        while position > 0:
            position -= 1
            file_obj.seek(position)
            if file_obj.read(1) == "\n":
                break
        return position


def load_config(config):
    """Klipper extras模块入口。"""
    return AIMPowerLossResume(config)
