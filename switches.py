from tkinter import *
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import threading
from threading import Lock
from tkinter.scrolledtext import ScrolledText
import socket,struct,threading,time,math
import serial, os
import time
from sys import exit
import sys, datetime, glob
from CStick import CStick
from CPrinter import CPrinter
import win32ui, win32con, win32print, win32gui
import re #регулярные выражения
import json
import queue
import ctypes
from cryptography.fernet import Fernet
import sv_ttk
import ros_api

CONFIG_FILE = "config.txt"  # Имя файла с конфигурацией по умолчанию
RESET_LOG_FILE = "switches_reset.log"  # Файл лога сброса коммутаторов

######################################################################
#            Функции                                                 #
######################################################################

def log_reset_to_file(action, vendor, model, mac, fw_version):
    """Записывает итог сброса/печати в файл лога."""
    try:
        print_log(f"---- Записываю в лог сброса: {action} | {vendor} | {model} | MAC: {mac} | FW: {fw_version}", visible=False)
        now = datetime.datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} | {action} | {vendor} | {model} | MAC: {mac} | FW: {fw_version}\n"
        with open(RESET_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception as e:
        print_log(f"---- Ошибка записи в лог: {e}", visible=False)
        pass  # Не мешаем работе программы, если файл не записался

######################################################################
#            Функции                                                 #
######################################################################

is_timeout = False
countdown_active = False
current_thread = None  # Будет хранить активный поток
ser = None  # Глобальная переменная для порта
button_lock = Lock()
is_sbros_busy = False
keyboard_bindings_active = True
auto_loop_active = False
restart_reset_requested = False  # Флаг: пользователь согласился перезапустить сброс на новой скорости
autospeed_locked = False  # Флаг: скорость COM уже определена автоматически и зафиксирована на текущую процедуру сброса
saved_autospeed = None  # Скорость, автоматически найденная в предыдущем цикле (для auto_loop)
saved_autospeed_valid = False  # Флаг: saved_autospeed содержит валидную скорость для следующего цикла

# Глобальные данные коммутатора для логирования
switch_data = {'vendor': '', 'model': '', 'mac': '', 'fw': ''}

class ResetTimeoutError(Exception):
    """Операция прервана по таймауту."""
    pass

def _run_on_main_thread(func):
    """Выполняет func в главном потоке Tkinter."""
    if threading.current_thread() is threading.main_thread():
        func()
    else:
        try:
            root.after(0, func)
        except NameError:
            func()

def safe_progress_set(value):
    """Потокобезопасная установка значения progressbar."""
    _run_on_main_thread(lambda: progress.set(value))

debug_raw_queue = queue.Queue()  # 'Сырые' данные для окна отладки: (kind, text), kind в {'output','input'}

def _timestamp():
    """Возвращает текущий тайм-код [hh:mm:ss.mmm]."""
    now = datetime.datetime.now()
    return f"[{now.strftime('%H:%M:%S')}.{now.microsecond // 1000:03d}]"

def _debug_push(kind, text):
    """Кладёт событие в очередь окна отладки, не блокируя поток чтения/записи COM-порта."""
    if text:
        debug_raw_queue.put((kind, text))

def serial_ports():  #Получить список  COM-портов в винде
    """ Lists serial port names
        :raises EnvironmentError:
            On unsupported or unknown platforms
        :returns:
            A list of the serial ports available on the system
    """
    if sys.platform.startswith('win'):
        ports = ['COM%s' % (i + 1) for i in range(256)]
    elif sys.platform.startswith('linux') or sys.platform.startswith('cygwin'):
        # this excludes your current terminal "/dev/tty"
        ports = glob.glob('/dev/tty[A-Za-z]*')
    elif sys.platform.startswith('darwin'):
        ports = glob.glob('/dev/tty.*')
    else:
        raise EnvironmentError('Unsupported platform')

    result = []
    for port in ports:
        try:
            s = serial.Serial(port)
            s.close()
            result.append(port)
        except (OSError, serial.SerialException):
            pass
    return result

def send( cmd, serial, reset=True ): #отправка текстовой команды в ком-порт
  if reset:
     serial.reset_input_buffer()
  serial.write( cmd.encode() )
  print( "send: %s" % cmd.encode() )
  _debug_push('input', cmd)

def read(pattern, serial ): #чтение вывода с ком-порта
   data_raw = b''
   try:
     print( ">wait: %s" % pattern )
     data_raw = serial.read_until(pattern.encode())
     print( "data readed %i" % len( data_raw ) )
   except serial.SerialTimeoutException:
     print_log( 'timeout!\n', visible=False)
   except serial.SerialException:
     print_log( 'serialexception', visible=False)
   return data_raw

def read_until(pattern, serial, timeout=5):
    global is_timeout
    
    if isinstance(pattern, str):
        pattern = [pattern]
    
    buf = ''
    timeout_ns = timeout * 1_000_000_000  # 5 сек в наносекундах
    is_timeout = False
    start = time.time_ns()

    while True:
        # Проверяем флаги остановки
        if stop_flags["SNR"] or stop_flags["Sbros"] or stop_flags["QTECH"]:
            print_log("**** Чтение из COM-порта остановлено пользователем...", visible=False)
            return {'pattern': '', 'timeout': True, 'buf': buf}

        # Если данных нет -> проверяем таймаут и ждём немного
        if not serial.in_waiting:
            if time.time_ns() - start > timeout_ns:
                is_timeout = True
                print("- timeout ожидания - ", pattern)
                return {'pattern': '', 'timeout': True, 'buf': buf}
            time.sleep(0.01)  # Чтобы не грузить CPU
            continue

        # Читаем данные (если они есть)
        try:
            ch = serial.read(1)
            decoded = ch.decode(errors='backslashreplace')
            buf += decoded
            print(decoded, end='', flush=True)
            _debug_push('output', decoded)

            # Проверяем паттерн
            for ptrn in pattern:
                srch = buf[-len(ptrn):]
                if srch == ptrn:
                    print(" <-- pattern '%s' --" % ptrn)
                    return {'pattern': ptrn, 'timeout': False, 'buf': buf}

        except Exception as e:
            print('decode exception:', e)
            continue

    return {'pattern': '', 'timeout': True, 'buf': buf}

def _looks_like_garbage(text):
    """Эвристика: похоже ли содержимое буфера на 'мусор' из-за неверной скорости COM-порта.
    При неправильном baud rate байты обычно превращаются в непечатаемые/случайные символы,
    что после decode(errors='backslashreplace') даёт много последовательностей вида \\xNN
    и управляющих символов вне обычных \\r \\n \\t.
    \\x08 (backspace) и \\x1b (ESC) не считаются мусором — используются для прогресс-баров и ANSI-кодов."""
    if len(text) < 8:
        return False
    escaped = text.count('\\x')  # каждая экранированная последовательность занимает 4 символа
    escaped_ratio = (escaped * 4) / len(text)
    control_chars = sum(1 for c in text if ord(c) < 32 and c not in '\r\n\t\x08\x1b')
    control_ratio = control_chars / len(text)
    return escaped_ratio > 0.4 or control_ratio > 0.3


ACTUAL_FIRMWARES_FILE = "actual_firmwares.txt"


def _load_actual_firmwares():
    """Загружает актуальные версии прошивок из actual_firmwares.txt.
    Формат файла: vendor|model|version (по одной записи на строку, # — комментарий).
    Возвращает список кортежей (vendor, model, version_str)."""
    result = []
    if not os.path.exists(ACTUAL_FIRMWARES_FILE):
        return result
    try:
        with open(ACTUAL_FIRMWARES_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('|')
                if len(parts) >= 3:
                    vendor = parts[0].strip()
                    model = parts[1].strip()
                    version = parts[2].strip()
                    result.append((vendor, model, version))
    except Exception as e:
        print(f"Ошибка чтения {ACTUAL_FIRMWARES_FILE}: {e}")
    return result


def _parse_version_tuple(ver_str):
    """Парсит строку версии в кортеж чисел для сравнения.
    Работает с полными версиями: "6.20.B30" -> (6,20,30), "8.2.1.238" -> (8,2,1,238),
    "4.04.B013" -> (4,4,13), "B007D" -> (7,). Берёт все группы цифр по порядку.
    Если не удалось распарсить — возвращает None."""
    if not ver_str:
        return None
    parts = re.findall(r'(\d+)', ver_str)
    if not parts:
        return None
    parts = [int(p) for p in parts]
    # Дополняем до 4 элементов нулями для корректного сравнения кортежей разной длины
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def print_firmware_info(vendor, model, version_str, buf):
    """Парсит версию прошивки из буфера и выводит в лог с подсветкой.
    Если версия не найдена — просто пропускает (сброс продолжается).
    vendor: "D-Link", "SNR", "QTECH"
    model: модель устройства (для поиска в actual_firmwares.txt). Может быть пустой —
           тогда модель ищется автоматически по подстрокам из файла в буфере buf.
    version_str: сырая строка версии, уже извлечённая из буфера
    buf: полный буфер для дополнительного парсинга (или None)"""
    if not version_str or version_str == "Unknown":
        print_log(f"---- Версия прошивки не определена", visible=False)
        return

    parsed = _parse_version_tuple(version_str)
    if parsed is None:
        print_log(f"---- Версия прошивки: {version_str} (не удалось распарсить)", color="orange")
        return

    # Загружаем актуальные версии из файла
    actual_firmwares = _load_actual_firmwares()

    # Ищем подходящую запись в файле.
    # Если model задана — совпадение по подстроке model.
    # Если model пустая — пытаемся найти модель из файла как подстроку в буфере.
    # Сортируем по длине модели (от длинных к коротким) — чтобы точные совпадения
    # (DGS-1210-28/ME) проверялись раньше общих (1210).
    sorted_firmwares = sorted(actual_firmwares, key=lambda x: len(x[1]), reverse=True)
    is_latest = False
    in_db = False
    matched_model = model
    buf_upper = (buf or "").upper()
    for fw_vendor, fw_model, fw_version in sorted_firmwares:
        if fw_vendor != vendor:
            continue
        if model:
            found = fw_model in model
        else:
            found = fw_model.upper() in buf_upper
        if found:
            in_db = True
            matched_model = fw_model
            known_ver = _parse_version_tuple(fw_version)
            if known_ver and parsed >= known_ver:
                is_latest = True
            break

    model_label = f" [{matched_model}]" if matched_model else ""
    if is_latest:
        print_log(f"🟢 Версия прошивки{model_label}: {version_str} (актуальная)", color="green")
    elif in_db:
        print_log(f"🟡 Версия прошивки{model_label}: {version_str} (рекомендуется обновить)", color="orange")
    else:
        # Модели нет в файле — показываем жёлтым, без рекомендации
        print_log(f"🟡 Версия прошивки{model_label}: {version_str}", color="orange")


def reconnect_port(new_speed):
    """Закрывает текущий COM-порт и сразу открывает его заново на новой скорости.
    Обновляет глобальную переменную ser, значение comspeed и подпись скорости в интерфейсе."""
    global ser
    try:
        if ser and hasattr(ser, 'is_open') and ser.is_open:
            ser.close()
            time.sleep(0.2)  # Даём Windows время освободить порт перед повторным открытием
    except Exception as e:
        print_log(f"---- Ошибка при закрытии порта перед переподключением: {e}", color="red")

    comspeed.set(new_speed)
    _run_on_main_thread(select_speed)  # Обновляем подпись "Скорость: ..." в интерфейсе

    ser = serial.Serial(comport.get(), new_speed, timeout=10)
    print_log(f"---- Порт переоткрыт на скорости {new_speed}", visible=False)
    return ser


def ask_yes_no_threadsafe(title, message):
    """Синхронный вызов messagebox.askyesno из фонового потока сброса.
    Блокирует вызывающий (фоновый) поток до тех пор, пока пользователь не ответит."""
    result = {'value': False}
    done = threading.Event()

    def _show():
        result['value'] = messagebox.askyesno(title, message)
        done.set()

    _run_on_main_thread(_show)
    done.wait()
    return result['value']


def guess_vendor_from_buf(buf):
    """Пытается по (возможно неполным) содержимому буфера понять, что за коммутатор перед нами,
    когда не хватило времени дождаться сброса целиком. Использует те же маркеры, что и основной
    разбор в reset_whatswitch."""
    if any(x in buf for x in ["Boot Procedure", "1210", "Power On Self Test", "MAC Address", "H/W Version"]):
        return "D-Link (3526/3200/3550/1210)"
    if any(x in buf for x in ["General initialization", "System is booting", "Bootrom version", "nos.img"]):
        return "SNR"
    if any(x in buf for x in ["is initializing", "Boot version:", "Press Ctrl-B", "System self-test", "Test OK.", "sending DISCOVER"]):
        return "QTECH QWS"
    return None


def read_until_autospeed(pattern, ser, timeout=120):
    """Аналог read_until, но с автоопределением скорости COM-порта.
    Если включен флажок 'Автоопределение скорости' и во входящих данных обнаружен
    нечитаемый 'мусор' (признак неверной скорости порта), порт немедленно закрывается
    и переоткрывается на другой скорости (9600<->115200). После переключения программа
    даёт устройству полноценное новое окно ожидания (столько же, сколько отводилось изначально) —
    некоторые коммутаторы грузятся долго — и продолжает пытаться распознать паттерн загрузки.
    Если время всё равно истекло, делается попытка угадать модель коммутатора по тому,
    что успело прийти в буфер.

    Возвращает словарь как read_until плюс:
      'ser'             — актуальный объект порта (мог измениться при переподключении)
      'speed_switched'  — было ли переподключение на другую скорость
      'new_speed'       — скорость, на которую переключились (если было переподключение)
      'guessed_vendor'  — предполагаемый тип коммутатора по неполным данным (или None)
    """
    global is_timeout, autospeed_locked

    if isinstance(pattern, str):
        pattern = [pattern]

    # Если скорость уже была автоматически зафиксирована ранее в этой процедуре сброса —
    # больше не переключаемся, даже если попадётся "мусор" (например, из-за перезагрузки коммутатора)
    autospeed_enabled = var_auto_speed.get() and not autospeed_locked
    buf = ''
    timeout_ns = timeout * 1_000_000_000
    is_timeout = False
    start = time.time_ns()
    garbage_checked_at = 0
    speed_switched = False
    new_speed = None
    switch_count = 0  # Счётчик переключений: разрешаем до 2 (туда-обратно)
    original_speed = comspeed.get()  # Запоминаем исходную скорость для возврата

    while True:
        if stop_flags["SNR"] or stop_flags["Sbros"] or stop_flags["QTECH"]:
            print_log("**** Чтение из COM-порта остановлено пользователем...", visible=False)
            return {'pattern': '', 'timeout': True, 'buf': buf, 'ser': ser,
                    'speed_switched': speed_switched, 'new_speed': new_speed,
                    'guessed_vendor': guess_vendor_from_buf(buf) if speed_switched else None}

        if not ser.in_waiting:
            if time.time_ns() - start > timeout_ns:
                is_timeout = True
                print("- timeout ожидания - ", pattern)
                return {'pattern': '', 'timeout': True, 'buf': buf, 'ser': ser,
                        'speed_switched': speed_switched, 'new_speed': new_speed,
                        'guessed_vendor': guess_vendor_from_buf(buf) if speed_switched else None}
            time.sleep(0.01)
            continue

        try:
            ch = ser.read(1)
            decoded = ch.decode(errors='backslashreplace')
            buf += decoded
            print(decoded, end='', flush=True)
            _debug_push('output', decoded)

            for ptrn in pattern:
                srch = buf[-len(ptrn):]
                if srch == ptrn:
                    print(" <-- pattern '%s' --" % ptrn)
                    return {'pattern': ptrn, 'timeout': False, 'buf': buf, 'ser': ser,
                            'speed_switched': speed_switched, 'new_speed': new_speed,
                            'guessed_vendor': None}

            # ---- Проверка на "мусор" (признак неверной скорости COM) ----
            # Разрешаем до 2 переключений: первое может быть ложным (dying gasp),
            # второе — возврат на исходную скорость
            if autospeed_enabled and switch_count < 2 and len(buf) - garbage_checked_at >= 12:
                tail = buf[-24:]
                garbage_checked_at = len(buf)
                if _looks_like_garbage(tail):
                    print_log("---- Обнаружен нечитаемый вывод COM-порта — похоже на неверную скорость", color="red", visible=False)
                    current_speed = comspeed.get()
                    if switch_count == 0:
                        # Первое переключение: пробуем альтернативную скорость
                        new_speed = 115200 if current_speed == 9600 else 9600
                    else:
                        # Второе переключение: возвращаемся на исходную
                        # (первое было ложным — dying gasp и т.п.)
                        new_speed = original_speed
                    print_log(f"---- Переподключаемся на скорости {new_speed}...", color="blue", visible=False)
                    try:
                        ser = reconnect_port(new_speed)
                    except Exception as e:
                        print_log(f"---- Не удалось переподключиться на {new_speed}: {e}", color="red")
                        return {'pattern': '', 'timeout': True, 'buf': buf, 'ser': ser,
                                'speed_switched': False, 'new_speed': None, 'guessed_vendor': None}
                    switch_count += 1
                    speed_switched = True
                    # Очищаем буфер — старые данные были на неверной скорости (мусор/dying gasp)
                    buf = ''
                    garbage_checked_at = 0
                    # После второго переключения блокируем — дальше не переключаемся
                    if switch_count >= 2:
                        autospeed_locked = True
                        saved_autospeed = new_speed
                        saved_autospeed_valid = True
                    print_log(f"---- Ждём загрузку устройства на скорости {new_speed}...", color="blue", visible=False)
                    continue

        except Exception as e:
            print('decode exception:', e)
            continue

    return {'pattern': '', 'timeout': True, 'buf': buf, 'ser': ser,
            'speed_switched': speed_switched, 'new_speed': new_speed, 'guessed_vendor': None}


_progress_anim_token = 0  # Увеличивается при каждом новом запуске reverse_progress/отмене - прерывает старые анимации

def cancel_progress_animation():
    """Прерывает любую ещё бегущую анимацию reverse_progress (например, оставшуюся от прошлой,
    уже завершённой операции), чтобы она не мешала прогресс-бару новой операции."""
    global _progress_anim_token
    _progress_anim_token += 1

def reverse_progress(): #Прогресс-бар в обратную сторону
    global _progress_anim_token
    current_value = progress.get()
    _progress_anim_token += 1
    own_token = _progress_anim_token

    def animate_progress():
        nonlocal current_value
        try:
            while current_value > 0 and _progress_anim_token == own_token:
                current_value = max(0, current_value - 1)  # Не опускаемся ниже 0
                safe_progress_set(current_value)
                time.sleep(0.5)
        except Exception as e:
            print(f"Ошибка в анимации прогресса: {e}")
    
    if current_value > 0:  # Запускаем только если есть что уменьшать
        threading.Thread(target=animate_progress, daemon=True).start()
    else:
        safe_progress_set(0)  # На всякий случай устанавливаем 0

def check_timeout():#проверяет таймаут, если не прошло - ошибка по таймауту
   global is_timeout
   if is_timeout:
      print_log("****  Выход по тайм-ауту")
      reverse_progress()
      safe_progress_set(0)
      for key in stop_flags:
            stop_flags[key] = True
      for key in what_print:
            what_print[key] = False
      is_timeout = False
      raise ResetTimeoutError("Operation timed out")

def flush_ser(ser): #принудительная очистка буфера
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.flush() #flush the buffer

def check_stop_flags(): #Проверяет, есть ли активные флаги остановки. Если да — завершает выполнение
    global stop_flags
    if any(stop_flags.values()):
        print_log("**** Сброс остановлен пользователем.")
        reverse_progress()
        return True  # Возвращает True, если нужно остановиться
    return False  # Иначе False

def ver_def_snr(ser):  # процедура сброса SNR после ребута
    global what_print, countdown_active #full_buf 
    progress.set(21) 
    # Ожидаем появления Bootrom version
    #data_raw = read_until(["Bootrom version", "Bootrom version:"], ser, 90)
    #print_log("\n---- Видим Bootrom version")    
    
    check_timeout()
    progress.set(30)

    if check_stop_flags():
        countdown_active = False
        return False
        
    data_raw = read_until("Testing RAM", ser, 180)
    print_log("\n---- Видим Testing RAM", visible=False)
    
    print("buf1 ", data_raw['buf'])
    print_log("---- Первая загрузка до Testing RAM есть", visible=False)
    check_timeout()
    progress.set(35)
    
    if check_stop_flags():
        countdown_active = False
        return False
        
    max_attempts = 40  # Максимальное количество попыток
    boot_menu_detected = False  # Флаг, что меню загрузки обнаружено
    print_log(f"---- Посылаем символы (максимум {max_attempts} штук)", visible=False)
    
    for attempt in range(max_attempts):
        send('\x02', ser, False)# Отправляем \x02 
        time.sleep(1)  # Ждём 1 сек перед чтением  
        
        if check_stop_flags():
            countdown_active = False
            return False
            
        # Проверяем ответ с таймаутом 0.1 сек
        data_raw = read_until(["[Boot]"], ser, 0.1)   
        if "[Boot]" in data_raw["buf"]:
            print_log("---- Обнаружено BOOT меню!", visible=False)
            progress.set(40)
            boot_menu_detected = True
            send(" \r\n", ser)
            time.sleep(1)
            break  # Выходим из цикла НЕМЕДЛЕННО
            
    if not boot_menu_detected:
        print_log(f"---- Не получилось зайти в BOOT меню после {max_attempts} ctrl+B", visible=False)
        print_log("---- Ожидаем [Boot] еще до 100 секунд.", visible=False)
        # Ожидаем BOOT-меню до 100 секунд
        data_raw = read_until(["[Boot]"], ser, 100)
        check_timeout()

        if "[Boot]" in data_raw["buf"]:
            print_log("---- Обнаружено BOOT меню в медленном ожидании!", visible=False)
            boot_menu_detected = True
        else:
            print_log(f"---- Не получилось зайти в BOOT меню. Таймаут 100 сек истёк.", visible=False)
            return False # Сброс не удался

    # Дополнительная отправка Enter, чтобы обновить консоль и получить приглашение
    send(" \r\n", ser)
    time.sleep(1)

    send("boot startup-config null\r\n", ser)
    data_raw = read_until([":"], ser, 2)
    check_timeout()
    print_log("---- Посылаем сброс из BOOT", visible=False)
    progress.set(50)

    send("run\r\n", ser)
    data_raw = read_until([":"], ser, 2)
    check_timeout()
    print_log("---- Применяем (run) из BOOT", visible=False)
    progress.set(55)

    print_log("\n---- ПЕРВАЯ ЧАСТЬ СБРОСА ЗАВЕРШЕНА", visible=False)

    data_raw = read_until(["initialization", "Loading flash"], ser, 120)
    print_log("---- Загружается, ждём", visible=False)
    progress.set(65)

    data_raw = read_until("Username:", ser, 200)
    send("admin\n", ser)
    data_raw = read_until("Password:", ser, 5)
    send("admin\n", ser)
    check_timeout()
    progress.set(75)
    
    send("\n", ser)
    send("\n", ser)
    send("\n", ser)
    data_raw = read_until("#", ser, 2)
    print_log("---- Вошли под логином и паролем", visible=False)

    check_timeout()
    send("show ver\r\n", ser)
    print_log("---- Проверка sho ver", visible=False)
    
    data_raw = read_until("Uptime", ser, 2)
    check_timeout()
    # Парсим версию прошивки SNR из show ver
    show_ver_buf = data_raw['buf']
    snr_ver_match = re.search(r'Version\s+(?:software\s+)?(\S+)', show_ver_buf, re.IGNORECASE)
    if snr_ver_match:
        snr_version = snr_ver_match.group(1).strip()
        print_firmware_info("SNR", "", snr_version, show_ver_buf)
        switch_data['fw'] = snr_version
    else:
        print_log("---- Версия прошивки SNR не найдена", visible=False)
        switch_data['fw'] = ''

    # Заполняем vendor, model, mac из show ver
    switch_data['vendor'] = 'SNR'
    for line in show_ver_buf.split('\n'):
        if 'Device' in line:
            m = re.search(r'(\S+)\s+Device,', line)
            if m:
                switch_data['model'] = m.group(1).strip()
                break
    for line in show_ver_buf.split('\n'):
        if 'MAC' in line or 'Hardware' in line:
            m = re.search(r'([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})', line)
            if m:
                switch_data['mac'] = m.group(1).upper()
                break
    print_log("---- Прочитали sho ver до uptime", visible=False)
    progress.set(80)
    
    print_log("\n---- ВЕСЬ СБРОС ЗАВЕРШЕН", visible=False)
    time.sleep(1)
    flush_ser(ser)
    return True

def ver_def_qtech( ser ):#процедура сброса QTECH после ребута
  global what_print, countdown_active #full_buf 
  data_raw = read_until( "Press Ctrl-B", ser, 60 )
  print_log("\n---- Приглашение в BOOT меню от QTECH", visible=False)
  check_timeout()
  progress.set(30)
  
  max_attempts = 10  # Максимальное количество попыток
  boot_menu_detected = False  # Флаг, что меню загрузки обнаружено
  print_log(f"---- Посылаем символы (максимум {max_attempts} штук)", visible=False)
  for attempt in range(max_attempts):
    send( '\x02', ser, False )  # Отправляем ctrl+B
    time.sleep(1)  # Ждём 0.5 сек перед чтением    
    # Проверяем ответ с таймаутом 0.1 сек
    data_raw = read_until(["Boot#"], ser, 0.1)    
    if "Boot#" in data_raw["buf"]: 
        print_log("---- Обнаружено Boot#", visible=False)
        progress.set(40)
        boot_menu_detected = True
        send(" \r\n", ser)
        time.sleep(1)
        break  # Выходим из цикла НЕМЕДЛЕННО
  if not boot_menu_detected:
    print_log(f"---- Не получилось зайти в BOOT меню после {max_attempts} ctrl+B", visible=False)
    check_timeout()

  data_raw = read_until( "Boot#", ser, 30 )
  send("bootloader startup-config NULL\r\n", ser )
  print_log("---- Посылаем команду обнуления", visible=False)
  data_raw = read_until( ["Boot#"], ser, 2 )
  check_timeout()
  progress.set(50)

  send("run\r\n", ser )
  print_log("---- Посылаем команду запуска", visible=False)
  data_raw = read_until( ["Loading flash"], ser, 2 )
  print_log("---- Пошла загрузка flash", visible=False)
  check_timeout()
  progress.set(60)

  print_log("\n---- ПЕРВАЯ ЧАСТЬ СБРОСА ЗАВЕРШЕНА", visible=False )
  
  data_raw = read_until( "sending DISCOVER package", ser, 200 )
  print_log("---- Загрузился и ищет DHCP для прошивки", visible=False)
  # Парсим версию прошивки QTECH из буфера после загрузки
  discover_buf = data_raw['buf']
  fw_match = re.search(r'Firmware Version[.\s]+([\d.]+)', discover_buf)
  if fw_match:
      fw_version = fw_match.group(1).strip()
      print_firmware_info("QTECH", "QSW-3500-10T-AC", fw_version, discover_buf)
      switch_data['fw'] = fw_version
  else:
      print_log("---- Версия прошивки QTECH не найдена в буфере", visible=False)
      switch_data['fw'] = ''

  # Заполняем vendor, model, mac из DISCOVER буфера
  switch_data['vendor'] = 'QTECH'
  for line in discover_buf.split('\n'):
      if 'Device' in line:
          m = re.search(r'Device:\s*(\S+)', line)
          if m:
              switch_data['model'] = m.group(1).strip()
              break
  for line in discover_buf.split('\n'):
      if 'MAC' in line:
          m = re.search(r'([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})', line)
          if m:
              switch_data['mac'] = r.group(1).upper()
              break
  progress.set(70)
  send( "\r\n", ser )
  data_raw = read_until( ">", ser, 5 )
  send( "enable\n", ser )
  data_raw = read_until( "#", ser, 5 )
  print_log("---- Зашли внутрь и видим #", visible=False)
  check_timeout()
  progress.set(80)
  
  print_log("\n---- ВЕСЬ СБРОС ЗАВЕРШЕН", visible=False)
  time.sleep(1)
  flush_ser(ser)
  return True

def ver_def1210ME( ser ):#процедура сброса 1210ME после ребута
  data_raw = read_until( ["100 %","100%", "MAC Address"], ser, 60 )
  print("buf1 ",data_raw['buf'])
  check_timeout()
  
  send( "^", ser, False )
  time.sleep(1)
  send( "^", ser, False )
  time.sleep(1)
  send( "^", ser, False )
  time.sleep(1)
  send( "^", ser, False )
  time.sleep(1)
  send( "^", ser, False )
  time.sleep(1)
  send( "^", ser, False )
  time.sleep(1)
  send( "^", ser, False )
  # des-1210ME response
  data_raw = read_until( ["F/W Version"], ser, 60 )
  print("buf1 ",data_raw['buf'])
  # Парсим версию прошивки из буфера
  fw_match = re.search(r'F/W Version\s*:\s*(\S+)', data_raw['buf'])
  if fw_match:
      fw_version = fw_match.group(1).strip()
      print_log(f"---- F/W Version: {fw_version}", visible=False)
      switch_data['fw'] = fw_version
  else:
      print_log("---- Версия прошивки D-Link 1210ME не найдена", visible=False)
      switch_data['fw'] = ''

  # Заполняем vendor, model, mac из буфера 1210ME
  switch_data['vendor'] = 'D-Link'
  switch_data['model'] = 'DGS-1210-28/ME'
  for line in data_raw['buf'].split('\n'):
      if 'MAC Address' in line:
          m = re.search(r'([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})', line)
          if m:
              switch_data['mac'] = m.group(1).replace('-', ':').upper()
              break
  data_raw = read_until( ["Password Recovery Mode."], ser, 30 )
  print( "@@@@ First recovery")
  check_timeout()
  
  data_raw = read_until( ["^",], ser, 60 )
  print("@@@ enter Recovery Mode" )
  check_timeout()
  send("\n", ser)
  data_raw = read_until( [">"], ser, 2 )
  check_timeout()
    
  ##########################################################
  send("reset config\n", ser )
  data_raw = read_until( ["(y/n)"], ser, 3 )
  check_timeout()
  send("y", ser )
  
  data_raw = read_until( ["UserName:","Username:"], ser, 120 )
  check_timeout()
  send( "\n", ser )

  data_raw = read_until( ["PassWord:","Password:"], ser )
  check_timeout() 
  send( "\n", ser )

  data_raw = read_until( "#", ser )
  check_timeout()
  
  return  

def ver_def1210MEA1( ser, full_buf ):#процедура сброса 1210ME после ребута
    max_attempts = 40  # Максимальное количество попыток
    boot_menu_detected = False  # Флаг, что меню загрузки обнаружено
    print_log(f"---- Посылаем символы (максимум {max_attempts} штук)", visible=False)
    for attempt in range(max_attempts):
        send("^", ser, False)  # Отправляем ^
        time.sleep(1)  # Ждём 1 сек перед чтением  
        data_raw = read_until(["The switch is currently", "Password Recovery Mode"], ser, 0.1)  
        full_buf += data_raw['buf'] # Наполняем наш буфер в цикле
        if "Password Recovery Mode" in data_raw['buf']:
            print_log("---- Обнаружено BOOT меню!", visible=False)
            progress.set(40)
            boot_menu_detected = True
            send(" \r\n", ser)
            time.sleep(1)
            break  # Выходим из цикла НЕМЕДЛЕННО
    if not boot_menu_detected:
        print_log(f"---- Не получилось зайти в BOOT меню после {max_attempts} ^", visible=False)
        check_timeout()

    # Парсим специфичные для 1210 поля
    hw_version = "Unknown"
    fw_version = "Unknown"
    mac_address = "Unknown"    

    lines = full_buf.split('\n')
    for line in lines:
        if "H/W Version" in line:
                hw_version = line.split("H/W Version : ")[-1].strip()
        elif "F/W Version" in line:
                fw_version = line.split("F/W Version : ")[-1].strip().split()[0]
        elif "MAC Address" in line:
                mac_address = line.split("MAC Address : ")[-1].strip()
    print_log(f"\n[D-Link 1210]", visible=False)
    print_log(f"MAC Address: {mac_address}", visible=False)
    print_log(f"H/W Version: {hw_version}", visible=False)
    print_log(f"F/W Version: {fw_version}\n", visible=False)
    print_firmware_info("D-Link", "DGS-1210-28/ME", fw_version, full_buf)
    switch_data['fw'] = fw_version
    switch_data['vendor'] = 'D-Link'
    switch_data['model'] = 'DGS-1210-28/ME'
    switch_data['mac'] = mac_address



    data_raw = read_until( [">","Mode"], ser, 60 )
    #print( "@@@", data_raw )
    if ">" in data_raw['buf']:
        print_log("---- Уже в рекавери", visible=False) 
    check_timeout()
    progress.set(50)

    send(" \r\n", ser )
    data_raw = read_until( [">"], ser, 1 )
    check_timeout()
    print_log("---- Начинаем сброс из recovery", visible=False)
    progress.set(55)

    send("reset config\n", ser ) 
    data_raw = read_until( ["(y/n)"], ser, 3 )
    check_timeout()
    time.sleep(1)
    send("y", ser ) 

    data_raw = read_until( ["Loading Runtime Image"], ser, 60 )
    check_timeout()
    print_log("---- Перезагрузился, ждём", visible=False)
    progress.set(60)

    data_raw = read_until( ["UserName:","Username:"], ser, 120 )
    check_timeout()
    send( "\n", ser )

    data_raw = read_until( ["PassWord:","Password:"], ser )
    check_timeout() 
    send( "\n", ser )   

    data_raw = read_until( "#", ser )
    check_timeout() 
    progress.set(80)
    print_log("---- Сброс 1210 A1 завершен", visible=False)

    return True


def ver_def3526(ser, image_version): #Конкретно под 3526
        data_raw = read_until( ["100 %","100%"], ser, 60 )
        print("buf1 ",data_raw['buf'])
        print_log("---- Первая загрузка до 100% есть", visible=False)
        check_timeout()
        progress.set(35)

        max_attempts = 40  # Максимальное количество попыток
        boot_menu_detected = False  # Флаг, что меню загрузки обнаружено
        print_log(f"---- Посылаем символы (максимум {max_attempts} штук)", visible=False)
        for attempt in range(max_attempts):
            send("#", ser, False)  # Отправляем #
            time.sleep(0.5)  # Ждём 0.5 сек перед чтением    
            # Проверяем ответ с таймаутом 0.1 сек
            data_raw = read_until(["Factory Default Enable"], ser, 0.1)    
            if "Factory Default Enable" in data_raw["buf"]: 
                print_log("---- Обнаружено Factory Default!", visible=False)
                progress.set(40)
                boot_menu_detected = True
                send(" \r\n", ser)
                time.sleep(1)
                break  # Выходим из цикла НЕМЕДЛЕННО
        if not boot_menu_detected:
            print_log(f"---- Не получилось зайти в BOOT меню после {max_attempts} ^", visible=False)
            check_timeout()

        data_raw = read_until( ["any key"], ser, 150 )
        print_log("---- Обнаружено  any key", visible=False) #Тут для 3526 начинаем сброс из основного меню
        print_log("---- Коммутатор сбросил пароли, продолжаем из меню", visible=False)
        progress.set(50)
        send( "\n", ser )

        data_raw = read_until( "username:", ser, 5 )
        check_timeout()
        send( "\n", ser )
        data_raw = read_until( "password:", ser, 5 )
        check_timeout()
        send( "\n", ser )
        data_raw = read_until( "#", ser )
        check_timeout()
        progress.set(60)

        send( "reset system force_agree\n", ser )
        print_log("---- Послали  команду сброса системы", visible=False)

        data_raw = read_until( ["Power On Self Test"], ser, 150 )
        print_log("---- Перезагрузился, тестирует себя", visible=False)
        progress.set(70)

        data_raw = read_until( "Press any key to login", ser, 150 )
        check_timeout()
        print_log("---- Приглашение нажать кнопочку", visible=False)
        progress.set(75)
        send( "\n", ser )

        data_raw = read_until( "username:", ser )
        check_timeout()
        send( "\n", ser )
        data_raw = read_until( "password:", ser )
        check_timeout()

        send( "\n", ser )
        data_raw = read_until( "#", ser)
        print_log("---- Снова вошли в 3526", visible=False)
        # 1. Отправляем команду "sh sw"
        send( "disable clipaging\n", ser )
        time.sleep(1) 
        send( "show switch\n", ser )
        # 2. Считываем ответ до следующего приглашения '#'
        # Используем большой таймаут на случай, если вывод команды большой
        data_raw = read_until( "#", ser, 10 ) 
        output = data_raw["buf"]

        # 3. Проверяем актуальность прошивки уже ПОСЛЕ сброса — тут видна и модель, и версия.
        #    Модель определяем автоматически по буферу show switch (Device Type: DES-3526...).
        fw_match = re.search(r'Firmware Version\s*:?\s*(?:Build\s+)?(\S+)', output, re.IGNORECASE)
        if fw_match:
            fw_version = fw_match.group(1).strip()
            print_firmware_info("D-Link", "", fw_version, output)
            switch_data['fw'] = fw_version
        else:
            print_log("---- Версия прошивки D-Link не найдена в выводе 'show switch'", visible=False)

        # Заполняем vendor, model, mac из show switch
        switch_data['vendor'] = 'D-Link'
        for line in output.split('\n'):
            if 'Device Type' in line:
                switch_data['model'] = line.split(':')[-1].strip()
                break
        for line in output.split('\n'):
            if 'MAC Address' in line:
                m = re.search(r'([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})', line)
                if m:
                    switch_data['mac'] = m.group(1).replace('-', ':').upper()
                break

        # 4. Ищем статус Power Status
        power_status_found = False
        for line in output.split('\n'):
            if "Power Status" in line:
                power_status_found = True

                if "Main - Normal" in line:
                    # Норма — ничего не показываем пользователю
                    print_log("---- Power Status Main - Normal" , color="green", visible=False)
                elif "Main - Abnormal" in line:
                    # Аномалия — выводим красным в основной лог
                    print_log("---- Power Status: Main - ABNORMAL ! ! !", color="red" )
                else:
                    # На случай, если статус отличается от Normal/Abnormal
                    print_log(f"---- Power Status: Неожиданное значение: {line.strip()}", visible=False)

                break # Нашли и проверили статус, выходим из цикла поиска по строкам

        if not power_status_found:
             print_log("---- ВНИМАНИЕ: Строка 'Power Status' не найдена в выводе 'sh sw'!", visible=False)
        progress.set(80)
        print_log("---- Сброс 3526 завершен", visible=False)
        return True    

def ver_def3200(ser, image_version): #Конкретно под 3200
        data_raw = read_until( ["100 %","100%"], ser, 50 )
        print("buf1 ",data_raw['buf'])
        print_log("---- Первая загрузка до 100% есть", visible=False)
        check_timeout()
        progress.set(35)

        max_attempts = 40  # Максимальное количество попыток
        boot_menu_detected = False  # Флаг, что меню загрузки обнаружено
        print_log(f"---- Посылаем символы (максимум {max_attempts} штук)", visible=False)
        for attempt in range(max_attempts):
            send("^", ser, False)  # Отправляем ^
            time.sleep(0.5)  # Ждём 0.5 сек перед чтением    
            # Проверяем ответ с таймаутом 0.1 сек
            data_raw = read_until(["Password Recovery Mode"], ser, 0.1)   
            if "Password Recovery Mode" in data_raw["buf"]:
                print_log("---- Обнаружено Recovery Mode меню!", visible=False)
                progress.set(40)
                boot_menu_detected = True
                send("\r\n", ser)
                time.sleep(0.5)
                send("\r\n", ser)
                time.sleep(0.5)
                break  # Выходим из цикла НЕМЕДЛЕННО
        if not boot_menu_detected:
            print_log(f"---- Не получилось зайти в BOOT меню после {max_attempts} ^", visible=False)
            check_timeout()
  
        data_raw = read_until( [">", "any key to login"], ser, 60 )
        print( "@@@", data_raw )
        if ">" in data_raw['buf']:
            print_log("---- Уже в рекавери", visible=False) 
        elif "any key to login" in data_raw['buf']:
            print_log("---- Вероятно выдало предупреждение! Обязательно обновить прошивку", visible=False)   
            send("\r\n", ser)
            time.sleep(0.5)
            send("\r\n", ser)
        check_timeout()
        progress.set(50)
  
        send(" \r\n", ser )
        data_raw = read_until( [">"], ser, 2 )
        check_timeout()
        print_log("---- Начинаем сброс из recovery", visible=False)
        progress.set(55)
  
        send("reset config\r\n", ser ) 
        data_raw = read_until( ["(y/n)"], ser, 3 )
        time.sleep(0.5)
        #check_timeout()
        send("y", ser )

        data_raw = read_until( [">"], ser, 45 )
        check_timeout()
        progress.set(60)

        send("reset account\r\n", ser )
        data_raw = read_until( [">", "(y/n)"], ser, 5 )
        if "(y/n)" in data_raw['buf']:
            send("y", ser )
            time.sleep(0.5)
        elif ">" in data_raw['buf']:  
            send(" \r\n", ser )  
        check_timeout()
        progress.set(65)

        data_raw = read_until( [">"], ser, 3 )
        check_timeout()

        send("reset password\r\n", ser )
        data_raw = read_until( [">"], ser, 5 )
        check_timeout()

        send("reboot \r\n", ser )
        data_raw = read_until( ["(y/n)"], ser, 3 )
        check_timeout()
        send("y", ser )
        progress.set(70)
        print_log("---- Сбросили и ожидаем перезагрузки", visible=False)

        data_raw = read_until( ["Power On Self Test"], ser, 120 )
        print_log("---- Перезагрузился, тестирует себя", visible=False)
        progress.set(75)
  
        data_raw = read_until( "any key to login", ser, 150 )
        check_timeout()
        send( "\n", ser )

        data_raw = read_until( ["UserName:","Username:","username"], ser, 120 )
        check_timeout()
        time.sleep(0.5)
  
        send( "\n", ser )
        data_raw = read_until( ["PassWord:","Password:","password"], ser )
        check_timeout()
        time.sleep(0.5)
        print_log("---- Вошли под логином и паролем", visible=False)
  
        send( "\n", ser )
        data_raw = read_until( "#", ser )

        check_timeout()
        progress.set(80)

        # Проверяем прошивку и Power Status уже после сброса — из show switch
        send( "disable clipaging\n", ser )
        time.sleep(1)
        send( "show switch\n", ser )
        data_raw = read_until( "#", ser, 10 )
        output = data_raw["buf"]

        # Парсим версию прошивки
        fw_match = re.search(r'Firmware Version\s*:?\s*(?:Build\s+)?(\S+)', output, re.IGNORECASE)
        if fw_match:
            fw_version = fw_match.group(1).strip()
            print_firmware_info("D-Link", "", fw_version, output)
            switch_data['fw'] = fw_version
        else:
            print_log("---- Версия прошивки D-Link не найдена в выводе 'show switch'", visible=False)

        # Заполняем vendor, model, mac из show switch
        switch_data['vendor'] = 'D-Link'
        for line in output.split('\n'):
            if 'Device Type' in line:
                switch_data['model'] = line.split(':')[-1].strip()
                break
        for line in output.split('\n'):
            if 'MAC Address' in line:
                m = re.search(r'([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})', line)
                if m:
                    switch_data['mac'] = m.group(1).replace('-', ':').upper()
                break

        # Проверяем Power Status
        power_status_found = False
        for line in output.split('\n'):
            if "Power Status" in line:
                power_status_found = True
                if "Main - Normal" in line:
                    print_log("---- Power Status Main - Normal", color="green", visible=False)
                elif "Main - Abnormal" in line:
                    print_log("---- Power Status: Main - ABNORMAL ! ! !", color="red")
                else:
                    print_log(f"---- Power Status: Неожиданное значение: {line.strip()}", visible=False)
                break
        if not power_status_found:
            print_log("---- ВНИМАНИЕ: Строка 'Power Status' не найдена в выводе 'sh sw'!", visible=False)

        return True    

def ver_defDlink( ser, pre_buf=None ):#общая процедура сброса Dlink после ребута
    global what_print, countdown_active #full_buf
    progress.set(21)
    
    if pre_buf:
        # Данные уже прочитаны (например, после автопереключения скорости) —
        # проверяем, есть ли в них нужные паттерны, и при необходимости дочитываем
        buf = pre_buf
        if "Kernel Image" not in buf and not any(x in buf for x in ["Runtime Image", "runtime image", "Runtime image"]):
            data_raw = read_until(["Runtime Image", "runtime image", "Runtime image", "Kernel Image"], ser, 60)
            buf += data_raw['buf']
        else:
            data_raw = {'buf': buf, 'timeout': False, 'pattern': ''}
    else:
        data_raw = read_until(["Runtime Image", "runtime image", "Runtime image" ,"Kernel Image"], ser, 60)
        buf = data_raw['buf']
    if "Kernel Image" in buf:  #1210ME A1 rev
        print_log("---- Предположительно DGS-1210ME", visible=False)
        data_raw = read_until(["100%"], ser, 60)
        #buf = data_raw['buf']
        try:
            progress.set(30)
            print_log("---- Запуск процедуры для D-Link 1210", visible=False)
            full_buf = "" # Инициализируем переменную для полного буфера
            full_buf += data_raw['buf'] #Начинаем наполнять буфером
            success = ver_def1210MEA1(ser, full_buf)
            if success: return True #Если процедура сброса прошла без ошибок
            else: return False
            
        except IndexError:
            print_log("---- Ошибка парсинга данных 1210. Нестандартный вывод.", visible=False)
            return False

    if not data_raw or 'buf' not in data_raw or not data_raw['buf']:
        print_log("Ошибка: не удалось получить версию образа", visible=False)
        countdown_active = False
        return False
    
    else:
        buf = data_raw['buf'].lower() #опускаем регистр, ибо в разных моделях скачут буквы в image_version
        if "please wait, loading " not in buf:
            print_log("Ошибка: не удалось определить версию образа (неожиданный вывод)", visible=False)
            return False
        image_version = buf.split("please wait, loading ")[1].split(" runtime image")[0].strip().upper() #режем и снова поднимаем регистр для красоты
        if "H/W Version   : " not in data_raw['buf']:
            print_log("Ошибка: не удалось определить H/W Version", visible=False)
            return False
        hw_version = data_raw['buf'].split("H/W Version   : ")[1].split("\n")[0].strip()

        print_log(f"\nH/W Version   : {hw_version}", visible=False)
        print_log(f"Runtime image : {image_version}\n", visible=False)
        # Пытаемся понять какой D-link пришел
        progress.set(25)

        if hw_version in ["A1", "B1", "C1"]:  # Точное сравнение  
            countdown_active = False
            progress.set(30)  
            if "C1" in hw_version: 
                print_log("---- Ревизия C1", visible=False)
            elif "A1" in hw_version or "B1" in hw_version:
                print_log("---- Ревизия A1/B1", visible=False) 
            # Версию прошивки проверяем ПОСЛЕ сброса в ver_def3200 (из show switch)
            print_log("---- Запуск процедуры для Длинков 3200 A1 B1 C1", visible=False)
            def_sucsess = ver_def3200(ser, image_version) 
            print_log(f"---- Статус сброса процедуры 3200 {def_sucsess}", visible=False)
            if def_sucsess: return True #Если процедура сброса прошла без ошибок
            else: return False
        
        elif hw_version in ["0A3G", "3A1", "A4", "A4G", "1A1"]:  # Точное сравнение
            countdown_active = False
            progress.set(30)  
            # Версию прошивки проверяем ПОСЛЕ сброса в ver_def3526 (из show switch)
            print_log("---- Запуск процедуры для Длинков 3526", visible=False)
            def_sucsess = ver_def3526(ser, image_version) 
            print_log(f"---- Статус сброса процедуры 3526 {def_sucsess}", visible=False)
            if def_sucsess: return True #Если процедура сброса прошла без ошибок
            else: return False 
        
        elif "#" in data_raw['buf']:
            print_log("---- Уже в коммутаторе (что-то пошло не так)", visible=False) #Но как мы там оказались?
            check_timeout()
            return False
            
        else:
            print_log("Неизвестная версия Dlink", visible=False)
            return False
  
  

def create_stick( data, fn, repair=False, spisanie=False):#процедура создания наклейки по заданным размерам 
   if not os.path.exists("sticks"):  #проверка сужествования папки sticks
    os.makedirs("sticks")
   stick1 = CStick( 336,200) #size in px
   stick1.addQR( data['mac'], 'center+30','center', 6 )

   if 12 < len(data['model']) < 17:  #если символов от 12 до 17
       stick1.addText( data['model'], posx=20, posy='center', size=20, rotate=90 )
   elif len(data['model'])>=17: #если символов больше или = 17
       stick1.addText( data['model'], posx=20, posy='center', size=15, rotate=90 )     
   else:    
       stick1.addText( data['model'], posx=20, posy='center', size=25, rotate=90 )
   if 12 < len(data['hwver']) < 17:  #если символов от 12 до 17
       stick1.addText( data['hwver'], posx=55, posy='center', size=20, rotate=90 )
   elif len(data['hwver'])>=17: #если символов больше или = 17
       stick1.addText( data['hwver'], posx=55, posy='center', size=15, rotate=90 )     
   else:    
       stick1.addText( data['hwver'], posx=55, posy='center', size=25, rotate=90 )
       
   stick1.addText( data['mac'], posx=85, posy='center', size=20, rotate=90 )
   timenow = datetime.datetime.today().strftime(f'%d %B %Y')
   stick1.addText( timenow, posx=300, posy='center', size=20, rotate=90 )

   stick1.create(repair, spisanie) #Создаём наклейку Ремонт если параметр не False
   stick1.save(fn)

def prn_stick_Data(data, repair=False, spisanie=False):  # процедура печати наклейки по заданным размерам 
    clear_text()
    if not repair and not spisanie:
        if data['mac'] == "": 
            print_log("Empty MAC", visible=False)
            raise ValueError("Пустой MAC-адрес")  # <--- Вызываем ошибку вместо тихого return
        if not validate_mac_input(data['mac']):
            print_log("Bad MAC", visible=False)
            raise ValueError("Некорректный MAC-адрес")  # <--- Тоже вызываем ошибку
    
    fn = "./sticks/stick_{}.png".format( data['mac'].replace(':','') )
    create_stick( data, fn, repair, spisanie )
    prn = CPrinter()
    curprn = selected_printer.get()  # Получаем выбранный принтер из combobox
    if not curprn:
        print_log("Принтер не выбран!", visible=False)
        raise ValueError("Принтер не выбран")  # <--- И здесь тоже
        
    prn.printfile(fn, printer_name=curprn)  # Передаём имя принтера
  #print("Реальная печать закоменчена!")


def check_loginpass_snr( ser, username, userpass ):#Процедура ввода логина/пароля если не залогинены в SNR и QTECH
       global is_timeout
       is_timeout = False
       flush_ser(ser) 
       send( username+"\n", ser )
       data_raw = read_until([":","#","!"], ser, 1 )
       #Есть 3 варианта ответов
       #1 Пришел запрос пароля
       if "Password:" in data_raw['buf']:
           send( f"{userpass}\n", ser )
           #Отправили админа, ждем ответ, 2 варианта
           data_raw = read_until([":","#","!"], ser, 1 )
           #1 Неправильный пароль или попытки
           if "Login invalid" in data_raw['buf']:
               print_log("Неправильный логин или пароль / попытки", visible=False)
               is_timeout = False
               return False
           #2 Мы зашли     
           elif  "#" in data_raw['buf']:
               print_log("Получилось залогиниться с вывода юзернейма", visible=False)
               is_timeout = False
               return True
           #3   
           elif "Username:" in data_raw['buf']:
               print_log("Что-то пошло не так, снова просит логин, значит неправильная пара", visible=False)
               is_timeout = False
               return False
       #2 Пришёл фейл (много попыток или неправильно)        
       elif "Login invalid" in data_raw['buf']:
          print_log("Неправильный логин/пароль/попытки", visible=False)
          is_timeout = False
          return False
       #3  Пришла решетка (хотя откуда ей взяться -_-) 
       elif "#" in data_raw['buf']:
          print_log("Получилось залогиниться с вывода юзернейма", visible=False)
          is_timeout = False
          return True
       else:
          print_log("Неизвестный ответ:", visible=False)
          is_timeout = False
       return False
         
def check_login_snr(ser, username="admin", userpass="admin"): #Процедура проверки залогиненности в SNR и QTECH
   global is_timeout
   is_timeout = False
   print_log( "...проверка залогиненности...", visible=False)
   send( "ena\n", ser )
   data_raw = read_until([":","#"], ser, 1)

   if "Password:" in data_raw['buf']:  # 2 nd
       print("1-st response")
       send("\n", ser )
       data_raw = read_until([":","#","$!"], ser, 1)
       
   if "Username:" in data_raw['buf']:  
       print("В начальном ответе юзернейм")
       #отлично, отправляем админа и читаем ответы       
       return check_loginpass_snr( ser, username, userpass )
   
          
   #3ий ответ - решетка  
   elif "#" in data_raw['buf']:   #3rd
       send( "\n", ser )
       print_log("Ура, уже залогинены", visible=False)
       #send( "exit\n", ser )
       is_timeout = False
       return True
   else:
       print_log("Неизвестный ответ:", visible=False)
       is_timeout = False
       return False

def check_login_dlink(ser, username="", userpass=""): #Процедура проверки залогиненности в SNR и QTECH
   global is_timeout
   is_timeout = False
   print_log( "---- Проверка залогиненности Dlink", visible=False)
   send( "\n", ser )
   time.sleep(0.1)
   progress.set(20)
   send( "\n", ser )
   time.sleep(0.1)
   progress.set(30)
   send( "\n", ser )
   time.sleep(0.1)
   progress.set(40)
   send( "\n", ser )
   time.sleep(0.1)
   progress.set(50)
   data_raw = read_until(["#", "sername:", "assword:"], ser, 2)

   if "#" in data_raw['buf']:
       print_log("---- Залогинены сразу", visible=False)
       progress.set(85)
       is_timeout = False
       return True
   elif "sername:" in data_raw['buf']:  
       if username =="" and userpass =="":  #пустые логины/пароли не подошли, если есть введенные, то юзаем их
            print_log("---- Заполните логин пароль и попробуйте снова", visible=False)
            is_timeout = False
            return False
       else: 
            print_log("---- sername в ответе", visible=False)
            send( f"{username}\n", ser )
            print_log("---- Послали введенный юзернейм (1)", visible=False)
            progress.set(70)
            data_raw = read_until(["assword"], ser, 1)
            send( f"{userpass}\n", ser )
            print_log("---- Послали введенный пароль (1)", visible=False)
            progress.set(80)
            data_raw = read_until(["#"], ser, 1)
            is_timeout = False
            if "#" in data_raw['buf']:
                return True
            return False
   elif "assword:" in data_raw['buf']:  
       if username =="" and userpass =="":
            print_log("---- Заполните логин пароль и попробуйте снова", visible=False)
            is_timeout = False
            return False
       else:
            print_log("---- password в ответе", visible=False)
            progress.set(60)
            send( "\n", ser )
            print_log("---- Послали enter чтобы перескочить на username", visible=False)
            data_raw = read_until(["sername"], ser, 1)
            send( f"{username}\n", ser )
            print_log("---- Послали введенный юзернейм (2) ", visible=False)
            progress.set(70)
            data_raw = read_until(["assword"], ser, 1)
            send( f"{userpass}\n", ser )
            print_log("---- Послали введенный пароль (2)", visible=False)
            progress.set(80)
            data_raw = read_until(["#"], ser, 1)
            is_timeout = False
            if "#" in data_raw['buf']:
                return True
            return False
   else:
       print_log("---- Что-то не тааак", visible=False)
       is_timeout = False
       return False


def generate_key():# Генерация ключа шифрования (если его нет)
    key = Fernet.generate_key()
    with open("secret.key", "wb") as key_file:
        key_file.write(key)

def load_key():# Загрузка ключа шифрования
    if not os.path.exists("secret.key"):
        generate_key()
    with open("secret.key", "rb") as key_file:
        return key_file.read()

def encrypt_data(data, key):# Шифрование данных
    fernet = Fernet(key)
    return fernet.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data, key):# Расшифровка данных
    if not encrypted_data:
        return ""
    fernet = Fernet(key)
    return fernet.decrypt(encrypted_data.encode()).decode()

def save_config(username, password, is_print, is_config, printer_name, auto_loop=False, auto_speed=False,
                mkt_ip='', mkt_user='', mkt_pass=''):
    key = load_key()
    config = {
        "username": username,
        "password": encrypt_data(password, key),  # Шифруем пароль
        "is_print": is_print,
        "is_config": is_config,
        "printer_name": printer_name,
        "auto_loop": auto_loop,
        "auto_speed": auto_speed,
        "mkt_ip": mkt_ip,
        "mkt_user": mkt_user,
        "mkt_pass": encrypt_data(mkt_pass, key)  # Шифруем пароль Mikrotik
    }
    with open("config.json", "w") as config_file:
        json.dump(config, config_file)

def load_config():
    if not os.path.exists("config.json"):
        return None, None, True, True, "", False, False, "172.16.50.1", "postadmin", ""  # Возвращаем пустую строку для принтера по умолчанию
    try:
        with open("config.json", "r") as config_file:
            config = json.load(config_file)
    except (json.JSONDecodeError, ValueError):
        # Файл повреждён — возвращаем значения по умолчанию
        return None, None, True, True, "", False, False, "172.16.50.1", "postadmin", ""
    key = load_key()
    username = config.get("username")
    password = decrypt_data(config.get("password"), key)  # Расшифровываем пароль
    is_print = config.get("is_print", True)  # Значение по умолчанию True
    is_config = config.get("is_config", True)  # Значение по умолчанию True
    printer_name = config.get("printer_name", "")  # Получаем имя принтера или пустую строку
    auto_loop = config.get("auto_loop", False)
    auto_speed = config.get("auto_speed", False)
    mkt_ip = config.get("mkt_ip", "172.16.50.1")
    mkt_user = config.get("mkt_user", "postadmin")
    mkt_pass = decrypt_data(config.get("mkt_pass", ""), key)  # Расшифровываем пароль Mikrotik
    return username, password, is_print, is_config, printer_name, auto_loop, auto_speed, mkt_ip, mkt_user, mkt_pass


def on_closing():
    global ser, current_thread, countdown_active

    # Сохраняем данные перед закрытием
    username = entryUser.get()
    password = entryPass.get()
    printer_name = selected_printer.get()
    save_config(username, password, is_print.get(), is_config.get(), printer_name,
                var_auto_loop.get(), var_auto_speed.get(),
                mkt_ip_var.get().strip(), mkt_user_var.get().strip(), mkt_pass_var.get().strip())

    # Останавливаем все операции
    for device in stop_flags:
        stop_flags[device] = True
    countdown_active = False

    # Ждём завершения потока (если он есть)
    if current_thread and current_thread.is_alive():
        current_thread.join(timeout=1.0)

    # Закрываем COM-порт
    if ser and hasattr(ser, 'is_open') and ser.is_open:
        try:
            ser.close()
        except Exception as e:
            print(f"Ошибка закрытия порта: {e}")

    # Очищаем окно отладки при закрытии
    try:
        debugOutput.delete("1.0", END)
    except NameError:
        pass

    root.destroy()

def stop_operation(device): #остановка оперций
    global ser, countdown_active, current_thread
    
    print_log(f"**** Останавливаю операцию для {device}...", visible=False)
    stop_flags[device] = True  # Устанавливаем флаг остановки
    countdown_active = False   # Останавливаем обратный отсчёт
    
    # Ждём завершения потока (если он есть)
    if current_thread and current_thread.is_alive():
        print_log("**** Ожидаем завершения потока...", visible=False)
        current_thread.join(timeout=2.0)  # Ждём не более 2 секунд
    
    # Безопасное закрытие порта
    if ser and hasattr(ser, 'is_open') and ser.is_open:
        try:
            ser.close()
            print_log("**** COM-порт успешно закрыт.", visible=False)
        except Exception as e:
            print_log(f"**** Ошибка закрытия порта: {e}")
    ser = None  # Обнуляем ссылку
    
    
    update_button_text(device) # Обновляем текст кнопки
    stop_flags[device] = False  # Возвращаем флаги на место
    print_log("**** Вернули стоп-флаги на место (False)", visible=False)


def update_button_text(device):
    btn_map = {
        "Sbros": (btnSbros, "1"),
    }
    if device in btn_map:
        btn, num = btn_map[device]
        _run_on_main_thread(lambda b=btn, n=num: b.config(text=f"{n}. Остановка..."))

def is_port_open(port):
    """Безопасная проверка состояния порта"""
    if port is None:
        return False
    try:
        return hasattr(port, 'is_open') and port.is_open
    except:
        return False

def prn_stick_snr(ser): #Печать наклейки SNR. Прогресс до 90
    
    global switch_data
    
    # Предполагается, что print_log, send, read_until, check_timeout, create_stick, 
    # CPrinter, selected_printer, progress и .get() определены в другом месте
    
    print_log( "---- печать наклейки SNR", visible=False )
    
    # 1. Сначала получаем вывод show ver, чтобы определить модель
    send( "show ver\n", ser )
    # Увеличиваем таймаут, так как нужно получить весь вывод, а затем определить модель для дальнейших действий
    data_ver_raw = read_until( "(C)", ser, 15 ) 
    check_timeout()
    
    lines_ver = data_ver_raw['buf'].split("\n")
    data = {'model':'','mac':'','hwver':'','serial':''}
    is_s2960 = False
    
    # Предварительный парсинг для определения модели и извлечения данных из 'show ver'
    for line in lines_ver:
        tmp = line.strip()
        print( tmp )
        
        # Определение модели и базовых данных
        if 'Device' in tmp:
            match_model = re.search(r'(\S+)\s+Device,', tmp)
            if match_model:
                data['model'] = match_model.group(1).strip()
                data['model2'] = data['model']
                print( f"##### Model number##### {data['model']}" )
                if 'S2960' in data['model']:
                    is_s2960 = True
        
        elif 'Device serial number' in tmp:
            match_serial = re.search(r'Device serial number\s+(\S+)', tmp)
            if match_serial:
                data['serial'] = match_serial.group(1).strip()
                print( f"##### Serial No.##### {data['serial']}" )
                
        elif 'HardWare Version' in tmp:
            match_hwver = re.search(r'HardWare Version\s+(\S+)', tmp)
            if match_hwver:
                data['hwver'] = match_hwver.group(1).strip()
                print( f"##### HardWare Version##### {data['hwver']}" )
                
        # Логика для старых моделей, которая может быть в исходном коде
        lst = re.split(r'[,.\n? ]+', tmp)
        if len(lst)>1:
            if 'Vlan MAC' in line and not is_s2960:
                print( "##### Vlan MAC (old logic)#####", lst[2] )
                # Предполагаем, что старая логика возвращает MAC в желаемом формате (с :)
                data['mac'] = lst[2]
            # ... другая старая логика, если нужна ...


    # 2. Если это S2960, выполняем 'show interface vlan 1' для получения MAC-адреса
    if is_s2960:
        print_log( "---- обнаружен S2960, получение MAC из 'sh int vlan 1'", visible=False )
        send( "show interface vlan 1\n", ser )
        # Ожидаем часть вывода, в которой содержится MAC-адрес
        data_mac_raw = read_until( "packets output", ser, 15 ) 
        check_timeout()
        
        lines_mac = data_mac_raw['buf'].split("\n")
        
        for line in lines_mac:
            tmp = line.strip()
            if 'Hardware is EtherSVI, address is' in tmp:
                # Регулярное выражение захватывает MAC в формате XX-XX-XX-XX-XX-XX
                match_mac = re.search(r'address is\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})', tmp)
                if match_mac:
                    mac_with_hyphen = match_mac.group(1).strip()
                    
                    # ИЗМЕНЕНИЕ: Заменяем дефисы на двоеточия
                    data['mac'] = mac_with_hyphen.replace('-', ':')
                    
                    print( f"##### MAC Address (S2960, standardized)##### {data['mac']}" )
                    break
    
    # 3. Финальная обработка и печать
    
    print( "---------------\n", data )
    
    if not data['mac']:
        print_log("Ошибка: MAC-адрес не найден! Прерывание.", visible=False)
        return
        
    # При формировании имени файла удаляем все разделители (двоеточия или дефисы, если вдруг остались)
    fn = "./sticks/stick_{}.png".format( data['mac'].replace(':','').replace('-','') )
    create_stick( data, fn )

    prn = CPrinter()
    curprn = selected_printer.get()  # Получаем выбранный принтер из combobox
    if not curprn:
        print_log("Принтер не выбран!", visible=False)
        return
    prn.printfile(fn, printer_name=curprn)  # Передаём имя принтера
    progress.set(90)
    #print("\n--------!!!Реальная Печать закомментирована!!-----------") 
    # Заполняем данные коммутатора для логирования
    switch_data['vendor'] = 'SNR'
    switch_data['model'] = data.get('model', '')
    switch_data['mac'] = data.get('mac', '')
    # Парсим версию прошивки из show ver
    snr_ver_match = re.search(r'Version\s+(?:software\s+)?(\S+)', data_ver_raw['buf'], re.IGNORECASE)
    switch_data['fw'] = snr_ver_match.group(1).strip() if snr_ver_match else ''

def prn_stick_qtech(ser): #Печать наклейки QTECH. Прогресс до 90
   global switch_data
   print_log( "---- Создаем наклейку", visible=False )
   send( "show ver\n", ser )
   data_raw = read_until( "Uptime", ser, 15 )
   check_timeout()
   lines = data_raw['buf'].split("\n")
   data={'model':'','mac':'','hwver':''}  #да тут напрашивается swver вместо hwver, но на этом много завязано
   for line in lines:
      if "Device:" in line:
         data['model'] = line.split("Device:")[1].split(",")[0].strip()
      if "VLAN MAC" in line:
          data['mac'] = line.split("VLAN MAC")[1].strip().replace("-", ":")
      if "SoftWare Version" in line:
          data['hwver'] = line.split("SoftWare Version")[1].strip()
   fn = "./sticks/stick_{}.png".format( data['mac'].replace(':','') )
   create_stick( data, fn )

   prn = CPrinter()
   curprn = selected_printer.get()  # Получаем выбранный принтер из combobox
   if not curprn:
       print_log("Принтер не выбран!", visible=False)
       return
   prn.printfile(fn, printer_name=curprn)  # Передаём имя принтера
   progress.set(90)
   print_log("---- Печать наклейки QTECH завершена", visible=False)
   #print_log("\n--------!!!Реальная Печать QTECH закомментирована!!")  
   # Заполняем данные коммутатора для логирования
   switch_data['vendor'] = 'QTECH'
   switch_data['model'] = data.get('model', '')
   switch_data['mac'] = data.get('mac', '')
   switch_data['fw'] = data.get('hwver', '')

def prn_stick_dlink(ser): #Печать наклейки Dlink. Прогресс до 90
  global switch_data
  print_log( "---- Создаем наклейку", visible=False )
  send( "\n", ser )
  send( "\n", ser )
  data_raw = read_until( "#", ser, 1 )
  check_timeout()
  
  send( "disable clipaging\n", ser )
  time.sleep(1) 
  
  send( "show switch\n", ser )
  data_raw = read_until( "#", ser )
  check_timeout()
  lines = data_raw['buf'].split("\n")
  data={'model':'','mac':'','hwver':''}
  for line in lines:
    tmp = line.split(":")
    if len(tmp)==2:
      if 'Device Type' in tmp[0].strip():
        tmp = tmp[1].strip().split(" ")[0]
        print( "##### Device Type", tmp )
        data['model'] = tmp
      if 'MAC Address' in tmp[0].strip():
        tmp = tmp[1].strip().replace("-",":")
        print( "##### MAC Address", tmp )
        data['mac'] = tmp
      if 'Hardware Version' in tmp[0].strip():
        tmp = tmp[1].strip()
        print( "##### HWVer Address", tmp )
        data['hwver'] = tmp
  fn = "./sticks/stick_{}.png".format( data['mac'].replace(':','') ) 
  create_stick( data, fn )   
  print( "---------------\n", data )
  send("enable clipaging\n", ser )
  # Заполняем данные коммутатора для логирования
  switch_data['vendor'] = 'D-Link'
  switch_data['model'] = data.get('model', '')
  switch_data['mac'] = data.get('mac', '')
  # Парсим версию прошивки из show switch
  fw_match = re.search(r'Firmware Version\s*:?\s*(?:Build\s+)?(\S+)', data_raw['buf'], re.IGNORECASE)
  switch_data['fw'] = fw_match.group(1).strip() if fw_match else ''

  prn = CPrinter()
  curprn = selected_printer.get()  # Получаем выбранный принтер из combobox
  if not curprn:
       print_log("Принтер не выбран!", visible=False)
       return
  prn.printfile(fn, printer_name=curprn)  # Передаём имя принтера
  progress.set(90)
  print_log("---- Печать наклейки dlink завершена", visible=False)
  #print_log("\n--------!!!Реальная Печать закомментирована!!-----------") 


def reset_whatswitch(ser):#приглашение к перезапуску неизвестного устройства. Прогресс с 10 до 80
    global what_print, countdown_active, restart_reset_requested  # Добавляем для изменения глобальной переменной

    try:
        what_print = {"DLINK": False, "SNR": False, "QTECH": False}  # Обнуляем на всякий начале функции
        if check_stop_flags():  
                return False
        
        # Запуск обратного отсчёта
        countdown_active = True
        countdown(120)  # 120 секунд

        if check_stop_flags():
                #countdown_active = False  
                return False
        progress.set(10)
        if check_stop_flags():  
                #countdown_active = False
                return False

        data_raw = read_until_autospeed(["Boot Procedure","General initialization","System is booting","is initializing", "1210", "Power On Self Test", "MAC Address", "H/W Version", "Boot version:", "Press Ctrl-B", "System self-test", "sending DISCOVER", "Uncompressing", "Bootrom version", "nos.img"], ser, 120)# Ожидание загрузки устройства (с автоопределением скорости COM)
        ser = data_raw.get('ser', ser)  # Порт мог измениться при автопереключении скорости
        if check_stop_flags(): 
                countdown_active = False 
                return False
        if data_raw.get('timeout') and data_raw.get('speed_switched'):
            countdown_active = False
            detected_speed = data_raw.get('new_speed') or comspeed.get()
            guessed_vendor = data_raw.get('guessed_vendor')
            if guessed_vendor:
                print_log(f"---- Скорость COM переключена на {detected_speed}. Похоже, это {guessed_vendor}, но сброс не успевает выполниться", color="red")
                message = (
                    f"Скорее всего, это коммутатор: {guessed_vendor}.\n"
                    f"Скорость COM переключена на {detected_speed}, но программа не успевает "
                    f"выполнить сброс — устройство грузится слишком долго.\n\n"
                    f"Перезагрузите коммутатор и подтвердите перезапуск сброса на скорости {detected_speed}."
                )
            else:
                print_log(f"---- Скорость COM переключена на {detected_speed}, но момент начала загрузки был пропущен", color="red")
                message = (
                    f"COM-порт переключен на скорость {detected_speed}, но момент начала "
                    f"загрузки коммутатора был пропущен.\n\nПерезапустить сброс на скорости {detected_speed}?"
                )
            restart = ask_yes_no_threadsafe("Автоопределение скорости", message)
            if restart:
                restart_reset_requested = True
            return False
        if data_raw.get('timeout'):
            # Таймаут без переключения скорости — ничего не подключено за 120 секунд
            countdown_active = False
            progressbar_label.config(text="Ничего не было подключено", foreground="red")
            return False
        if not data_raw or 'buf' not in data_raw:
            print_log("Ошибка: timeout или нет данных" , color="red")
            countdown_active = False
            return False
        progress.set(15)

        buf = data_raw['buf']
        #if "DGS-1210" in buf:
        if any(x in buf for x in ["Boot Procedure", "1210", "Power On Self Test", "MAC Address", "H/W Version"]):
            print_log("---- Обнаружен Dlink 3526/3200/3550/1210" , color="green", visible=False)
            what_print["DLINK"] = True
            countdown_active = False
            progressbar_label.config(text="ВЫПОЛНЯЮ СБРОС", foreground="black")
            print_log("---- Запуск процедуры для Длинков", visible=False)
            progress.set(20)
            verdef_success = ver_defDlink(ser) #Запускаем сброс через переменную, чтобы потом проверить её статус
            if check_stop_flags():
                countdown_active = False
                return False
            if not verdef_success:
                print_log("---- Сброс Длинк не удался", visible=False)
                countdown_active = False
                return False
            else:
                print_log("---- Сброс Длинк успешен", visible=False)
                return True
            
        elif any(x in buf for x in ["Boot version:", "Press Ctrl-B", "System self-test", "sending DISCOVER"]):
            what_print["QTECH"] = True
            countdown_active = False
            progressbar_label.config(text="ВЫПОЛНЯЮ СБРОС", foreground="black")
            print_log("---- Запуск процедуры для QTECHов", visible=False)
            progress.set(20)
            verdef_success3 = ver_def_qtech(ser) #Запускаем сброс через переменную, чтобы потом проверить её статус
            if check_stop_flags():
                countdown_active = False
                return False
            if not verdef_success3:
                print_log("---- Сброс QTECH не удался", visible=False)
                countdown_active = False
                return False
            else:   
                print_log("---- Сброс QTECH успешен", visible=False)
                return True

        elif any(x in buf for x in ["General initialization", "System is booting", "Bootrom version", "nos.img"]):
            what_print["SNR"] = True
            countdown_active = False
            progressbar_label.config(text="ВЫПОЛНЯЮ СБРОС", foreground="black")
            print_log("---- Запуск процедуры для SNRов", visible=False)
            progress.set(20)
            verdef_success2 = ver_def_snr(ser) #Запускаем сброс через переменную, чтобы потом проверить её статус
            if check_stop_flags():
                countdown_active = False
                return False
            if not verdef_success2:
                print_log("---- Сброс SNR не удался", visible=False)
                countdown_active = False
                return False
            else:
                print_log("---- Сброс SNR успешен", visible=False)
                return True
            
        elif any(x in buf for x in ["is initializing"]):
            what_print["QTECH"] = True
            countdown_active = False
            progressbar_label.config(text="ВЫПОЛНЯЮ СБРОС", foreground="black")
            print_log("---- Запуск процедуры для QTECHов", visible=False)
            progress.set(20)
            verdef_success3 = ver_def_qtech(ser) #Запускаем сброс через переменную, чтобы потом проверить её статус
            if check_stop_flags():
                countdown_active = False
                return False
            if not verdef_success3:
                print_log("---- Сброс QTECH не удался", visible=False)
                countdown_active = False
                return False
            else:   
                print_log("---- Сброс QTECH успешен", visible=False)
                return True


        elif data_raw.get('speed_switched'):
            # После переключения скорости буфер очищен, но известный паттерн совпал.
            # Не потребляем данные лишним read_until — переходим к процедуре сброса,
            # она сама прочитает то, что нужно (данные ещё в COM-буфере).
            countdown_active = False
            matched_pattern = data_raw.get('pattern', '')

            # Uncompressing = D-Link 1210 (вывод загрузчика)
            if "Uncompressing" in buf:
                print_log("---- Обнаружен Dlink 1210 (Uncompressing после переключения скорости)", color="green", visible=False)
                what_print["DLINK"] = True
                progressbar_label.config(text="ВЫПОЛНЯЮ СБРОС", foreground="black")
                print_log("---- Запуск процедуры для Длинков", visible=False)
                progress.set(20)
                verdef_success = ver_defDlink(ser, buf)
                if check_stop_flags():
                    return False
                if not verdef_success:
                    print_log("---- Сброс Длинк не удался", visible=False)
                    return False
                else:
                    print_log("---- Сброс Длинк успешен", visible=False)
                    return True

            if any(x in buf for x in ["Boot Procedure", "1210", "Power On Self Test", "MAC Address", "H/W Version"]):
                print_log("---- Обнаружен Dlink 3526/3200/3550/1210", color="green", visible=False)
                what_print["DLINK"] = True
                progressbar_label.config(text="ВЫПОЛНЯЮ СБРОС", foreground="black")
                print_log("---- Запуск процедуры для Длинков", visible=False)
                progress.set(20)
                verdef_success = ver_defDlink(ser, buf)
                if check_stop_flags():
                    return False
                if not verdef_success:
                    print_log("---- Сброс Длинк не удался", visible=False)
                    return False
                else:
                    print_log("---- Сброс Длинк успешен", visible=False)
                    return True

            elif any(x in buf for x in ["Boot version:", "Press Ctrl-B", "System self-test", "sending DISCOVER"]):
                what_print["QTECH"] = True
                progressbar_label.config(text="ВЫПОЛНЯЮ СБРОС", foreground="black")
                progress.set(20)
                verdef_success3 = ver_def_qtech(ser)
                if check_stop_flags():
                    return False
                if not verdef_success3:
                    print_log("---- Сброс QTECH не удался", visible=False)
                    return False
                else:
                    print_log("---- Сброс QTECH успешен", visible=False)
                    return True

            elif any(x in buf for x in ["General initialization", "System is booting", "Bootrom version", "nos.img"]):
                what_print["SNR"] = True
                progressbar_label.config(text="ВЫПОЛНЯЮ СБРОС", foreground="black")
                progress.set(20)
                verdef_success2 = ver_def_snr(ser)
                if check_stop_flags():
                    return False
                if not verdef_success2:
                    print_log("---- Сброс SNR не удался", visible=False)
                    return False
                else:
                    print_log("---- Сброс SNR успешен", visible=False)
                    return True

            elif any(x in buf for x in ["is initializing"]):
                what_print["QTECH"] = True
                progressbar_label.config(text="ВЫПОЛНЯЮ СБРОС", foreground="black")
                progress.set(20)
                verdef_success3 = ver_def_qtech(ser)
                if check_stop_flags():
                    return False
                if not verdef_success3:
                    print_log("---- Сброс QTECH не удался", visible=False)
                    return False
                else:
                    print_log("---- Сброс QTECH успешен", visible=False)
                    return True

            else:
                # Не удалось определить коммутатор по buf (он содержит только совпавший паттерн).
                # Фиксируем скорость как правильную и просим перезагрузить.
                detected_speed = data_raw.get('new_speed') or comspeed.get()
                autospeed_locked = True
                saved_autospeed = detected_speed
                saved_autospeed_valid = True
                guessed_vendor = guess_vendor_from_buf(buf)
                if guessed_vendor:
                    print_log(f"---- Скорость COM зафиксирована на {detected_speed}. Похоже на {guessed_vendor}, но сброс не успел начаться", color="red")
                    message = (
                        f"Скорость COM зафиксирована на {detected_speed}.\n"
                        f"Похоже, это коммутатор: {guessed_vendor}, но программа не успела "
                        f"перехватить момент начала загрузки.\n\n"
                        f"Перезагрузите коммутатор и подтвердите перезапуск сброса."
                    )
                else:
                    print_log(f"---- Скорость COM зафиксирована на {detected_speed}, но коммутатор не опознан", color="red")
                    message = (
                        f"Скорость COM зафиксирована на {detected_speed}, но коммутатор "
                        f"не был опознан за отведённое время.\n\n"
                        f"Перезагрузите коммутатор и подтвердите перезапуск сброса."
                    )
                progressbar_label.config(text="НЕ ОПОЗНАН — перезагрузите коммутатор", foreground="red")
                restart = ask_yes_no_threadsafe("Автоопределение скорости", message)
                if restart:
                    restart_reset_requested = True
                return False

        else:
            progressbar_label.config(text="Сброс/отменён или не удался, печать наклейки пропущена.", foreground="red")
            return False   

    except serial.SerialException as e:
        print_log(f"1Ошибка COM-порта: {e}")
        _run_on_main_thread(lambda: messagebox.showerror("Ошибка", f"1Ошибка COM-порта: {e}"))
        return False
    except ResetTimeoutError:
        print_log("---- Операция прервана по таймауту")
        countdown_active = False
        return False
    except Exception as e:
        print_log(f"Ошибка: {e}")
        _run_on_main_thread(lambda: messagebox.showerror("Ошибка", f"Ошибка: {e}"))
        return False
    finally:
        progress.set(80)
        

def ensure_serial_connection(ser): # функция для проверки и восстановления соединения
    if not ser.is_open:
        print_log("Соединение с COM-портом закрыто. Открываем заново...", visible=False)
        ser.open()
    return ser

def upload_config(ser, config_file): #отправка дефолтной конфиги после сброса из файла 1-7.txt
    try:
        # ПРОВЕРЯЕМ СУЩЕСТВОВАНИЕ ФАЙЛА
        if not os.path.exists(config_file):
            print_log(f"Файл конфигурации '{config_file}' не найден.", color="red", visible=False)
            return False
        # Проверяем и восстанавливаем соединение
        ser = ensure_serial_connection(ser)
        # ОЧИЩАЕМ БУФЕР ПЕРЕД ОТПРАВКОЙ КОМАНД
        flush_ser(ser)
        print_log(f"Загружаем конфигурацию из файла: {config_file}", visible=False)

        with open(config_file, 'r', encoding='utf-8') as file:  # Указание кодировки
            config_lines = file.readlines()
            print_log("---- Отправка команд конфигурации", visible=False)
            for line in config_lines:
                # Пропускаем пустые строки и комментарии
                clean_line = line.strip()
                if not clean_line or clean_line.startswith('!'):
                    continue

                command = clean_line + "\r\n"  # Используйте \r\n для совместимости с Cisco-like CLI
                #print_log(f"Отправка: {command.strip()}")
                send(command, ser)
                time.sleep(0.2)  # Увеличьте задержку, если команды не успевают обрабатываться
                # Можете добавить чтение ответа после каждой команды для надежности:
                # read_until(["#", ">"], ser, 2)  
        print_log("Конфигурация успешно отправлена.", visible=False)
        return True
    except Exception as e:
        print_log(f"Ошибка при отправке конфигурации: {e}", color="red", visible=False)
        return False


def do_reset_whatswitch(is_print, is_config):#МНОГОПОТОЧНОСТЬ Кнопка 1
    global ser, countdown_active, current_thread, config_file, autospeed_locked, saved_autospeed, saved_autospeed_valid, comspeed
    
    # Сначала закрываем старый COM-порт если он остался открытым
    if ser and hasattr(ser, 'is_open') and ser.is_open:
        try:
            ser.close()
            print_log("---- Принудительно закрыт оставленный COM-порт", color="blue", visible=False)
            time.sleep(0.3)  # Даём Windows освободить порт
        except Exception as e:
            print_log(f"---- Ошибка закрытия перед повторным открытием: {e}", color="blue")
    
    ser = None
    autospeed_locked = False  # Новая процедура сброса — снова разрешаем автоопределение скорости при необходимости
    
    # Если включён автоцикл и есть сохранённая скорость из предыдущего цикла — используем её
    if var_auto_loop.get() and saved_autospeed_valid and saved_autospeed is not None:
        print_log(f"---- Автоцикл: используем сохранённую скорость {saved_autospeed} из предыдущего цикла", color="blue")
        comspeed.set(saved_autospeed)
        # Сбрасываем флаг после использования — скорость нужна только для ОДНОГО следующего цикла
        saved_autospeed_valid = False
    
    cancel_progress_animation()  # Прерываем "обратный" отсчёт прогресс-бара, оставшийся от прошлой операции
    safe_progress_set(3)
    reset_success = False
    try:
        if check_stop_flags():
                return False 
        clear_text()
        _run_on_main_thread(lambda: btnSbros_text.set("Остановить сброс"))
        print_log( "---- Выполняем определение коммутатора\n", color="blue", visible=False)
        safe_progress_set(5)

        # Открываем COM-порт
        ser = serial.Serial(comport.get(), comspeed.get(), timeout=10)
        # Очистка буфера — гарантируем что старые данные не попадут в автоопределение
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        print_log(f"---- COM-порт открыт на скорости {comspeed.get()}", visible=False)
        
        # Основной цикл сброса
        if not stop_flags["Sbros"]:  # Проверяем перед каждой долгой операцией
            reset_success = reset_whatswitch(ser)
            if not reset_success:
                progressbar_label.config(text="Сброс/отменён или не удался, печать наклейки пропущена.", foreground="red")
                return
       
        if is_print and not stop_flags["Sbros"]:  # Проверяем перед печатью
                if what_print["DLINK"]:
                    print_log("---- Печать наклейки D-Link", visible=False)
                    prn_stick_dlink(ser)
                elif what_print["SNR"]:
                    print_log("---- Печать наклейки SNR", visible=False)
                    prn_stick_snr(ser)  
                elif what_print["QTECH"]:   
                    print_log("---- Печать наклейки QTECH", visible=False) 
                    prn_stick_qtech(ser)    
                else:
                    print_log("---- Не выбрана модель для печати наклейки.", visible=False)   
        else: 
            print_log("---- Печать не нужна", visible=False) 

        if is_config and not stop_flags["Sbros"]:  # Используем .get() для BooleanVar
            # Конфиг загружаем ТОЛЬКО для SNR и QTECH (для D-Link это не нужно)
            if what_print.get("SNR") or what_print.get("QTECH"):
                print_log(f"---- Заливаем конфиг для {('SNR' if what_print.get('SNR') else 'QTECH')}", visible=False)
                upload_config(ser, CONFIG_FILE)
            else:
                print_log("---- Пропуск конфига: D-Link не требует загрузки конфигурации", color="blue", visible=False)    
        else: 
            print_log("---- Заливка конфига не нужна", visible=False)    
        
    except serial.SerialException as e:
        if not stop_flags["Sbros"]:  # Не логируем ошибки, если остановка по флагу
            print_log(f"Ошибка COM-порта: {e}", color="red")
    except Exception as e:
        print_log(f"Неожиданная ошибка: {e}", color="red")
    finally:
        def _finish_reset():
            global restart_reset_requested
            with button_lock:
                if stop_flags["Sbros"]:
                    btnSbros_text.set("1. Сброс к заводским (остановлено)")
                    var_auto_loop.set(False)
                    restart_reset_requested = False
                else:
                    btnSbros_text.set("1. Сброс к заводским")
                    if restart_reset_requested:
                        restart_reset_requested = False
                        root.after(1000, lambda: click_btnSbros(None))
                    elif var_auto_loop.get():
                        root.after(3000, lambda: click_btnSbros(None))
            enable_buttons()
        _run_on_main_thread(_finish_reset)
        if reset_success:
            progressbar_label.config(text="СБРОШЕН К ЗАВОДСКИМ", foreground="black")
            safe_progress_set(100)
            time.sleep(3)
            # Записываем итог в лог
            action = "Сброс+наклейка" if is_print else "Сброс"
            log_reset_to_file(action, switch_data['vendor'], switch_data['model'], switch_data['mac'], switch_data['fw'])
        if ser and hasattr(ser, 'is_open') and ser.is_open:
            flush_ser(ser)
            ser.close()
            print_log("---- COM порт закрыт", visible=False)
        safe_progress_set(0)
        _run_on_main_thread(_flash_taskbar)



def _flash_taskbar():
    """Мигает иконкой программы в панели задач Windows для привлечения внимания."""
    try:
        # Используем ctypes для FlashWindow — не требует win32gui
        hwnd = root.winfo_id()
        ctypes.windll.user32.FlashWindow(hwnd, True)
    except Exception:
        pass


def _fix_idle_speed_if_needed(ser):
    """Проверяет, что скорость COM подобрана верно, пока коммутатор уже включён и стоит на
    приглашении (в отличие от момента загрузки, здесь можно спокойно перебирать скорости, не
    боясь опоздать за выводом консоли). Для печати наклейки работает всегда, независимо от
    чекбокса 'Автоопределение скорости COM' (тот чекбокс касается только процедуры сброса).
    Возвращает актуальный объект ser (мог измениться при переподключении)."""
    global autospeed_locked
    for speed_try in range(2):  # Всего два варианта скорости - 9600 и 115200
        # На каждой скорости отправляем до 3х Enter с короткими паузами —
        # иногда коммутатор не отвечает на первый символ (особенно после BootROM)
        # read_until() останавливается сразу, как только находит символ приглашения,
        # так что если коммутатор ответил — цикл прерывается немедленно
        found_prompt = False
        for enter_attempt in range(3):
            send("\n", ser)
            time.sleep(0.1)  # Короткая пауза перед чтением ответа
            data_raw = read_until(["#", ":", ">", "$"], ser, 1)  # Таймаут 1 сек (было 3)
            buf = data_raw['buf']
            # Проверяем наличие конкретного символа приглашения
            # Если коммутатор ответил на этой скорости, в буфере будет #, :, > или $
            if buf and ('#' in buf or ':' in buf or '>' in buf or '$' in buf):
                found_prompt = True
                break
        if found_prompt:
            return ser  # Найдено приглашение — скорость верная, переходим к печати
        
        # После 3х Enter на этой скорости ничего не ответило — пробуем другую скорость
        new_speed = 115200 if comspeed.get() == 9600 else 9600
        print_log(f"---- Нет ответа на этой скорости, пробуем {new_speed}...", color="blue")
        try:
            ser = reconnect_port(new_speed)
            autospeed_locked = True
        except Exception as e:
            print_log(f"---- Не удалось переподключиться на {new_speed}: {e}", color="red")
            return ser
    return ser


def do_prn_stick_auto(is_print): #Автоопределение производителя подключенного коммутатора и печать наклейки
    """Определяет производителя коммутатора, который уже подключен (и, как правило, уже залогинен),
    и печатает соответствующую наклейку. Порядок: сначала аккуратно пробуем D-Link, подтверждая его
    по содержимому ответа на 'show switch'. Если это не подтвердилось — пробуем SNR/QTECH (у них общий
    вход в систему) и различаем их по содержимому ответа на 'show ver'. Наклейка печатается только если
    ожидаемые поля реально нашлись в ответе — иначе сообщаем, что не смогли определить модель, вместо
    того чтобы напечатать наклейку с пустыми данными."""
    global autospeed_locked, ser
    ser = None
    for key in stop_flags:  # Иначе таймаут в предыдущей попытке навсегда блокирует все следующие чтения
        stop_flags[key] = False
    cancel_progress_animation()  # Прерываем "обратный" отсчёт прогресс-бара, оставшийся от прошлой операции
    progressbar_label.config(text="", foreground="black")
    try:
        ser = serial.Serial(comport.get(), comspeed.get(), timeout=10)
        print_log("---- Определяем производителя подключенного коммутатора...", visible=False)
        autospeed_locked = False
        
        # Подбираем скорость (отправляет 2 Enter, читает ответы для каждой скорости)
        ser = _fix_idle_speed_if_needed(ser)
        
        # ---- Шаг 0: проверяем, подключён ли коммутатор вообще ----
        print_log("---- Проверка подключения коммутатора...", visible=False)
        if not ser or not hasattr(ser, 'is_open') or not ser.is_open:
            print_log("---- COM-порт недоступен. Коммутатор не подключён.", color="red", visible=False)
            _run_on_main_thread(lambda: messagebox.showerror(
                "Не подключено",
                "COM-порт недоступен.\nПроверьте подключение конвертера UART к USB."
            ))
            safe_progress_set(0)
            if ser:
                try: ser.close()
                except: pass
            return
        
        # После подбора скорости — отправляем 2 Enter и ждём вразимый ответ
        ser.reset_input_buffer()  # Чистим остатки от _fix_idle_speed
        send("\n", ser)
        time.sleep(0.2)
        send("\n", ser)
        data_test = read_until(["#", ":", ">", "$"], ser, 3)
        
        if not data_test['buf'] or _looks_like_garbage(data_test['buf']):
            print_log("---- Нет ответа от коммутатора. Убедитесь, что он включён и подключён.", color="red", visible=False)
            _run_on_main_thread(lambda: messagebox.showerror(
                "Нет ответа",
                "Коммутатор не отвечает.\n\n"
                "Проверьте:\n"
                "• Устройство включено\n"
                "• Кабель подключён\n"
                "• COM-порт выбран правильно"
            ))
            safe_progress_set(0)
            try: ser.close()
            except: pass
            return
        
        print_log("---- Коммутатор подключён и отвечает", color="green", visible=False)
        # Очищаем буфер после проверки
        ser.reset_input_buffer()

        already_logged_in_unknown = False  # Дошли до "#", но это может быть не D-Link

        # ---- Шаг 1: пробуем как D-Link ----
        if check_login_dlink(ser, entryUser.get(), entryPass.get()):
            send("disable clipaging\n", ser)  # Иначе длинный вывод 'show switch' упрётся в '--More--' и зависнет
            time.sleep(1)
            send("show switch\n", ser)
            data_raw = read_until("#", ser, 10)
            if "Device Type" in data_raw['buf'] or "MAC Address" in data_raw['buf']:
                print_log("---- Определён D-Link", color="green", visible=False)
                safe_progress_set(90)
                prn_stick_dlink(ser)
                send("logout\n", ser)
                log_reset_to_file("Наклейка", switch_data['vendor'], switch_data['model'], switch_data['mac'], switch_data['fw'])
                return
            else:
                print_log("---- Не похоже на D-Link — пробуем SNR/QTECH", visible=False)
                already_logged_in_unknown = True  # Мы всё ещё залогинены, просто это другой производитель

        # ---- Шаг 2: пробуем как SNR/QTECH (общий вход в систему) ----
        if not already_logged_in_unknown:
            already_logged_in_unknown = check_login_snr(ser, "admin", "admin")
            if not already_logged_in_unknown and entryUser.get() and entryPass.get():
                already_logged_in_unknown = check_login_snr(ser, entryUser.get(), entryPass.get())

        if not already_logged_in_unknown:
            print_log("---- Не удалось определить производителя коммутатора", color="red", visible=False)
            _run_on_main_thread(lambda: messagebox.showwarning(
                "Не определено",
                "Не удалось распознать производителя коммутатора.\nПроверьте логин/пароль и подключение."
            ))
            return

        send("show ver\n", ser)
        data_raw = read_until(["(C)", "Uptime"], ser, 15)
        vbuf = data_raw['buf']

        if "Device," in vbuf:
            print_log("---- Определён SNR", color="green", visible=False)
            prn_stick_snr(ser)
        elif "Device:" in vbuf:
            print_log("---- Определён QTECH", color="green", visible=False)
            prn_stick_qtech(ser)
        else:
            print_log("---- Залогинились, но не смогли распознать модель по ответу 'show ver'", color="red", visible=False)
            _run_on_main_thread(lambda: messagebox.showwarning(
                "Не определено", "Авторизация прошла, но не удалось распознать модель коммутатора."
            ))
            return

        send("exit\n", ser)
        # Записываем итог в лог
        log_reset_to_file("Наклейка", switch_data['vendor'], switch_data['model'], switch_data['mac'], switch_data['fw'])

    except serial.SerialException as e:
        print_log(f"Ошибка COM-порта: {e}", color="red")
        _run_on_main_thread(lambda: messagebox.showerror("Ошибка", f"Ошибка COM-порта: {e}"))
    except Exception as e:
        print_log(f"Ошибка: {e}", color="red")
        _run_on_main_thread(lambda: messagebox.showerror("Ошибка", f"Ошибка: {e}"))
    finally:
        _run_on_main_thread(lambda: btnPrintStickAuto.config(text="Печать QR подключенного коммутатора"))
        safe_progress_set(0)
        if ser and hasattr(ser, 'is_open') and ser.is_open:
            ser.close()

def check_comport():# проверка существования свободных COM-портов
  if comport.get()=="":
     print_log("COM порт не задан!", color="red")
     return False
  return True 

def prn_stick_mikrotik():
    """Подключается к Mikrotik по API, получает список интерфейсов,
    показывает диалог выбора MAC, генерирует наклейку и печатает."""
    ip = mkt_ip_var.get().strip()
    username = mkt_user_var.get().strip()
    password = mkt_pass_var.get().strip()
    if not ip:
        messagebox.showwarning("Mikrotik", "Введите IP-адрес роутера")
        return

    print_log("---- Подключение к Mikrotik по API...", visible=False)
    try:
        router = ros_api.Api(ip, user=username, password=password)
        ifaces = router.talk('/interface/print')
        ident = router.talk('/system/identity/print')
        rb = router.talk('/system/routerboard/print')

        model = rb[0].get('model', 'Unknown') if rb else 'Unknown'
        identity_name = ident[0].get('name', '') if ident else ''

        # Собираем интерфейсы с MAC-адресами
        iface_list = []
        for iface in ifaces:
            mac = iface.get('mac-address', '')
            name = iface.get('name', '')
            if mac and mac != '00:00:00:00:00:00':
                iface_list.append((name, mac))

        if not iface_list:
            print_log("---- Нет интерфейсов с MAC-адресом", color="red", visible=False)
            messagebox.showwarning("Mikrotik", "Не найдено интерфейсов с MAC-адресом")
            return

        # Диалог выбора интерфейса
        sel = _mkt_select_iface(iface_list)
        if sel is None:
            print_log("---- Выбор интерфейса отменён", visible=False)
            return

        mac_address = sel[1]
        print_log(f"---- Mikrotik: {model}, MAC: {mac_address}", color="green", visible=False)

        # Генерация наклейки
        data = {'mac': mac_address, 'model': model, 'hwver': identity_name}
        if not os.path.exists("sticks"):
            os.makedirs("sticks")
        fn = f"./sticks/stick_mkt_{mac_address.replace(':','')}.png"

        stick1 = CStick(336, 200)
        stick1.addQR(data['mac'], 'center+30', 'center', 6)
        stick1.addText(data['model'], posx=20, posy='center', size=25, rotate=90)
        stick1.addText(data['hwver'], posx=55, posy='center', size=25, rotate=90)
        stick1.addText(data['mac'], posx=85, posy='center', size=20, rotate=90)
        timenow = datetime.datetime.today().strftime('%d %B %Y')
        stick1.addText(timenow, posx=300, posy='center', size=20, rotate=90)
        stick1.create()
        stick1.save(fn)

        # Печать
        curprn = selected_printer.get()
        if not curprn:
            print_log("Принтер не выбран!", color="red", visible=False)
            messagebox.showwarning("Mikrotik", "Принтер не выбран")
            return
        prn = CPrinter()
        prn.printfile(fn, printer_name=curprn)
        print_log("---- Наклейка Mikrotik отправлена на печать", visible=False)

    except Exception as e:
        print_log(f"---- Ошибка Mikrotik: {e}", color="red", visible=False)
        messagebox.showerror("Mikrotik", f"Ошибка: {e}")


def _mkt_select_iface(iface_list):
    """Показывает модальный диалог выбора интерфейса Mikrotik.
    Возвращает кортеж (name, mac) или None при отмене."""
    dlg = tk.Toplevel(root)
    dlg.title("Выбор интерфейса Mikrotik")
    dlg.geometry("400x300")
    dlg.transient(root)
    dlg.grab_set()

    result = {'sel': None}

    tree = ttk.Treeview(dlg, columns=('name', 'mac'), show='headings')
    tree.heading('name', text='Интерфейс')
    tree.heading('mac', text='MAC-адрес')
    tree.pack(fill='both', expand=True, padx=10, pady=2)

    for name, mac in iface_list:
        tree.insert('', 'end', values=(name, mac))

    def on_ok():
        sel = tree.selection()
        if sel:
            vals = tree.item(sel[0])['values']
            result['sel'] = (vals[0], vals[1])
        dlg.destroy()

    def on_cancel():
        dlg.destroy()

    btn_frame = ttk.Frame(dlg)
    btn_frame.pack(pady=2)
    ttk.Button(btn_frame, text="OK", command=on_ok).pack(side='left', padx=5)
    ttk.Button(btn_frame, text="Отмена", command=on_cancel).pack(side='left', padx=5)

    dlg.wait_window()
    return result['sel']


def click_btnPrintMikrotik(event=None):
    btnPrintMikrotik.config(state='disabled')
    btnPrintMikrotik.config(text="Подключение...")
    btnPrintMikrotik.update()
    try:
        prn_stick_mikrotik()
    finally:
        btnPrintMikrotik.config(text="Печать наклейки Mikrotik")
        btnPrintMikrotik.config(state='normal') 

def select_speed(): #показываем выбранную скорость в лейбле
    global saved_autospeed, saved_autospeed_valid
    speed_header.config(text=f"Скорость: {comspeed.get()}")
    # При ручном переключении скорости сбрасываем автосохранение
    saved_autospeed = None
    saved_autospeed_valid = False
    

stop_flags = {
    "Sbros": False,
    "SNR": False,
    "QTECH": False,
}

what_print = {
    "DLINK": False,
    "SNR": False,
    "QTECH": False,
}



def click_btnSbros(event=None): # кнопка Sbros 1 
    global is_sbros_busy, current_thread
    if is_sbros_busy:
        return
    is_sbros_busy = True
    try:
        with button_lock:
            if btnSbros_text.get() == "1. Сброс к заводским":
                disable_buttons()
                btnCheckCom.config(state='disabled') # Её блочим отдельно только при вызове кнопки Сброс
                btnSbros.config(state='normal')
                btnSbros_text.set("Остановить сброс")
                for key in stop_flags:  # Сбрасываем все флаги - иначе застрявший после другой операции флаг заблокирует сброс
                    stop_flags[key] = False
                current_thread = threading.Thread(target=do_reset_whatswitch, args=(is_print.get(), is_config.get(),))
                current_thread.start()
            else:
                stop_operation("Sbros")
                enable_buttons()
                var_auto_loop.set(False)
    finally:
        is_sbros_busy = False

     
def click_btnPrintStickAuto(event=None):#кнопка печати наклейки с автоопределением
    global current_thread
    btnPrintStickAuto.config(text="Определяем коммутатор...")
    btnPrintStickAuto.update()  # Принудительное обновление текста кнопки
    current_thread = threading.Thread(target=do_prn_stick_auto, args=(is_print.get(),))
    current_thread.start()

def click_btnPrintRemont(event=None): # кнопка r
    clear_text()
    btnPrintRemont["text"] = "Нажата Ремонт"
    btnPrintRemont.update()  # Принудительное обновление текста кнопки
    try:
        data = {'model':'', 'mac':'', 'hwver':''}
        data["model"] = "Требуется"
        data["mac"] = ""
        data["hwver"] = "ремонт"
        prn_stick_Data(data, repair=True)
        print_log("---- Наклейка отправлена на печать", visible=False)
        
    except Exception as e:  # Ловим любые исключения, включая отмену печати
        print_log(f"Ошибка печати: {e}", visible=False)
        
    finally:  # Этот блок выполнится ВСЕГДА, независимо от того, была ошибка или нет
        btnPrintRemont["text"] = "R. Наклейка Ремонт"
        btnPrintRemont.update()

def click_btnPrintSpisanie(event=None): # кнопка x
    btnPrintSpisanie["text"] = "Нажата Списание"
    clear_text()
    btnPrintSpisanie.update()  # Принудительное обновление текста кнопки
    try:
        data = {'model':'', 'mac':'', 'hwver':''}
        data["model"] = "Требуется"
        data["mac"] = ""
        data["hwver"] = "списание"
        prn_stick_Data(data, spisanie=True)
        print_log("---- Наклейка отправлена на печать", visible=False)
        
    except Exception as e:  # Ловим любые исключения, включая отмену печати
        print_log(f"Ошибка печати: {e}", visible=False)
        
    finally:  # Этот блок выполнится ВСЕГДА, независимо от того, была ошибка или нет
        btnPrintSpisanie["text"] = "X. Наклейка Списание"
        btnPrintSpisanie.update()

def click_btnPrintStickData(event=None): # кнопка печати из введенных данных
    btnPrintStickData["text"] = "Нажата ИзДанных"
    clear_text()
    btnPrintStickData.update()  # Принудительное обновление текста кнопки
    try:
        data = {'model':'', 'mac':'', 'hwver':''}
        # Добавить проверки сущ полей и их значений
        data["model"] = entryModel.get().strip()
        data["mac"] = entryMac.get().strip()
        data["hwver"] = entrySerial.get().strip()
        prn_stick_Data(data)
        print_log("---- Наклейка отправлена на печать", visible=False)
        
    except Exception as e:  # Ловим любые исключения, включая отмену печати
        print_log(f"Ошибка печати: {e}", visible=False)   
    finally:  # Этот блок выполнится ВСЕГДА, независимо от того, была ошибка или нет
        btnPrintStickData["text"] = "↑ Наклейка из данных"
        btnPrintStickData.update()

def click_btnCheckCom(): # Кнопка проверки ком-портов
    disable_buttons()
    clear_text()
    progressbar_label.config(text="", foreground="black")
    btnPrintRemont.config(state='normal')
    btnPrintStickData.config(state='normal')
    btnPrintSpisanie.config(state='normal')
    if check_com_ports():
        comports = serial_ports()
        print_log(f"На момент {time.strftime('%H:%M:%S')} доступны порты: {comports}")


def disable_buttons():
    # btnCheckCom.config(state='disabled') # Её блочим отдельно только при вызове кнопки Сброс
    btnSbros.config(state='disabled')
    btnPrintRemont.config(state='disabled')
    btnPrintSpisanie.config(state='disabled')
    btnPrintStickData.config(state='disabled')
    btnPrintStickAuto.config(state='disabled')
    btnPrintMikrotik.config(state='disabled')
    enabled_checkbutton.config(state='disabled')
    config_checkbutton.config(state='disabled')
    radio_btn9600.config(state='disabled')
    radio_btn115200.config(state='disabled')
    comport_cb.config(state='disabled')
    chkAutoLoop.config(state='disabled')
    chkAutoSpeed.config(state='disabled')

def enable_buttons():
    btnCheckCom.config(state='normal')
    btnSbros.config(state='normal')
    btnPrintRemont.config(state='normal')
    btnPrintSpisanie.config(state='normal')
    btnPrintStickData.config(state='normal')
    btnPrintStickAuto.config(state='normal')
    btnPrintMikrotik.config(state='normal')
    enabled_checkbutton.config(state='normal')
    config_checkbutton.config(state='normal')
    radio_btn9600.config(state='normal')
    radio_btn115200.config(state='normal') 
    comport_cb.config(state='normal')  
    chkAutoLoop.config(state='normal')  
    chkAutoSpeed.config(state='normal')

def countdown(seconds): #Обновляет прогресс-бар и текст отсчёта внутри него.
    global countdown_active
    if seconds > 0 and countdown_active:  # Проверка, активен ли отсчёт
        progressbar_label.config(text=f"Ожидается перезагрузка устройства... {seconds} сек.")
        # Дублируем в окно отладки
        print_log(f"---- Ожидается перезагрузка устройства... {seconds} сек.", update=True, visible=False)
        if seconds % 2 == 0:
            progressbar.step(1)
        else:
           progressbar.step(-1)     
        if seconds > 0:  # Продолжаем отсчёт, если время не истекло
            root.after(1000, countdown, seconds - 1)           
    else:
        countdown_active = False  # Останавливаем отсчёт
        # Не очищаем текст — его устанавливает вызвавшая сторона

def print_log(message, update=False, color=None, visible=True): #Вывод сообщения в лог с возможностью обновления
    def _do_log():
        if visible:
            clean_msg = message.replace("---- ", "").replace("\n", "").strip()
            fg_color = "#2C3E50"
            if color == "green": fg_color = "#27ae60"
            elif color == "red": fg_color = "#e74c3c"
            elif color == "orange": fg_color = "#d35400"
            elif color == "blue": fg_color = "#2980b9"
            try:
                progressbar_label.config(text=clean_msg, foreground=fg_color)
            except NameError:
                pass


        # Дублируем это же процедурное сообщение в окно отладки (если оно уже создано)
        try:
            debugOutput.insert(END, f"{_timestamp()} {message}" + "\n", ("proc",))
            debugOutput.yview(END)
        except NameError:
            pass

    _run_on_main_thread(_do_log)

def clear_text():   #очистка поля вывода при нажатии кнопки
    def _do_clear():
        progressbar_label.config(text="", foreground="black")
        # debugOutput больше не очищается — данные сохраняются между циклами
    _run_on_main_thread(_do_clear)

def _drain_debug_queue():
    """Каждые ~150 мс переносит накопленные 'сырые' данные COM-порта и отправленные команды
    в окно отладки одним пакетом — так не грузим GUI на каждый отдельный символ."""
    output_chunks = []
    input_chunks = []
    try:
        while True:
            kind, text = debug_raw_queue.get_nowait()
            if kind == 'output':
                output_chunks.append(text)
            elif kind == 'input':
                input_chunks.append(text)
    except queue.Empty:
        pass

    try:
        if output_chunks:
            raw = ''.join(output_chunks)
            # Экранируем нечитаемые байты для отображения в отладке
            escaped = []
            for ch in raw:
                code = ord(ch)
                if code < 32 and ch not in ('\n', '\r', '\t'):
                    escaped.append(f'\\x{code:02x}')
                elif code > 126 and code < 160:
                    escaped.append(f'\\x{code:02x}')
                else:
                    escaped.append(ch)
            escaped_text = ''.join(escaped)
            # Помечаем, если текст был экранирован
            if escaped_text != raw:
                debugOutput.insert(END, f"{_timestamp()} [НЕЧИТАЕМЫЕ СИМВОЛЫ ЭКРАНИРОВАНЫ]\n", ("proc",))
            debugOutput.insert(END, f"{_timestamp()} {escaped_text}\n", ("output",))
        for cmd in input_chunks:
            debugOutput.insert(END, f"{_timestamp()} >>> ОТПРАВЛЕНО: {cmd!r}\n", ("input",))
        if output_chunks or input_chunks:
            debugOutput.yview(END)

    except NameError:
        pass

    root.after(150, _drain_debug_queue)


def on_keypress(event=None): #Биндим клавиши под кнопки   
    global keyboard_bindings_active
    # Если обработка клавиш отключена или активны другие элементы
    if (not keyboard_bindings_active or 
        (ser and hasattr(ser, 'is_open') and ser.is_open) or
        (event and event.widget.winfo_class() in ['TEntry', 'Text'])):
        return

    if event.char=='1':
        click_btnSbros()  
    elif event.char=='p':
        click_btnPrintStickAuto()
    elif event.char=='r':
        click_btnPrintRemont() 
    elif event.char=='x':
        click_btnPrintSpisanie()            
    
    return
    

def validate_mac_input(new_text): # Регулярное выражение для проверки MAC-адреса
   
    # Поддерживает форматы: XX:XX:XX:XX:XX:XX, XX-XX-XX-XX-XX-XX, XXXX.XXXX.XXXX (Cisco)
    pattern = r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$|^([0-9A-Fa-f]{4}\.){2}([0-9A-Fa-f]{4})$'
    return bool(re.match(pattern, new_text))
def on_entry_change(event):# Если не валиден мак после ввода, предупреждаем
    # Получаем текущий текст из Entry
    current_text = entryMac.get()
    # Проверяем, валиден ли MAC-адрес
    if not validate_mac_input(current_text):
        # Если не валиден, предупреждаем
        entryMac.delete(len(current_text) - 1, tk.END) #добавить новый лейбл предупреждения


def check_com_ports():  #Проверка доступности COM портов для граф интерфейса
    global keyboard_bindings_active
    comports = serial_ports()
    if not comports:
        keyboard_bindings_active = False
        retry = messagebox.askretrycancel(
            "COM-порт info", 
            "COM-порты не найдены или заняты!\n"
            "1. Проверьте подключение устройства\n"
            "2. Закройте другие программы, использующие COM-порт"       
        )
        disable_buttons() #Блочим все кнопки, кроме тех, что не участвуют в ком-портах
        btnPrintRemont.config(state='normal')
        btnPrintStickData.config(state='normal')
        btnPrintSpisanie.config(state='normal')
        if retry:
            keyboard_bindings_active = True
            return check_com_ports()  # Рекурсивный вызов
        return False
    else:
        keyboard_bindings_active = True
        enable_buttons()
        comports = serial_ports()
        comport.set(comports[0] if len(comports) > 0 else "")
        comport_cb.config(textvariable=comport, values=comports, state="readonly")
        print("Отрисовка во время функции")
        print(comport)
    return True


class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip = None
        self.widget.bind("<Enter>", self.show_tooltip)
        self.widget.bind("<Leave>", self.hide_tooltip)

    def show_tooltip(self, event=None):
        """Показывает подсказку."""
        if self.tooltip:
            return

        # Позиционируем подсказку рядом с курсором
        x, y, _, _ = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 25

        # Создаем окно подсказки
        self.tooltip = tk.Toplevel(self.widget)
        self.tooltip.wm_overrideredirect(True)  # Убираем рамку и заголовок
        self.tooltip.wm_geometry(f"+{x}+{y}")

        # Добавляем текст в подсказку
        label = tk.Label(self.tooltip, text=self.text, bg="lightyellow", relief="solid", borderwidth=1)
        label.pack()

    def hide_tooltip(self, event=None):
        """Скрывает подсказку."""
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None

####################################################################################
#                            Графический интерфейс                                 #
####################################################################################

root =  Tk() #окно приложения
sv_ttk.set_theme("light")
root.title('Сброс и печать наклеек v1.08043')
root.geometry("750x500+400+200")
root.minsize(750, 450) # Слегка увеличили окно для более просторных отступов

# --- НАЧАЛО БЛОКА ВИЗУАЛЬНОГО ОФОРМЛЕНИЯ ---
style = ttk.Style()
# Включаем плоскую тему (встроена в Tkinter), избавляемся от дизайна Windows 95
if 'clam' in style.theme_names():
    pass #style.theme_use('clam')

# Принудительно делаем фон Combobox белым для всех состояний
style.map('TCombobox', 
    fieldbackground=[('readonly', 'white'), ('disabled', '#f0f0f0')],
    selectbackground=[('readonly', '#0078d7')], # Цвет выделения текста (синий стандартный)
    selectforeground=[('readonly', 'white')]
)

# Если нужно, чтобы и обычный белый фон поля ввода (не readonly) был белым:
style.configure('TCombobox', fieldbackground='white', background='white')

# Базовая цветовая палитра
BG_COLOR = "#F4F6F9"      # Современный светло-серый фон
TEXT_COLOR = "#2C3E50"    # Темно-сине-серый цвет текста для мягкого контраста
ACCENT_COLOR = "#3498DB"  # Синий акцент для главной кнопки

root.configure(bg=BG_COLOR)
root.attributes("-alpha", 0.97) # Чуть меньшая прозрачность для читаемости

# Глобальная настройка шрифтов и цветов для всех виджетов ttk
app_font = ('Segoe UI', 9)
style.configure('.', font=app_font)
#style.configure('TFrame', background=BG_COLOR)
#style.configure('TLabel', background=BG_COLOR, foreground=TEXT_COLOR)
#style.configure('TCheckbutton', background=BG_COLOR, foreground=TEXT_COLOR)
#style.configure('TRadiobutton', background=BG_COLOR, foreground=TEXT_COLOR)

# Стили для обычных кнопок
style.configure('TButton', font=app_font, padding=4)
#style.map('TButton', background=[('active', '#E2E8F0')], foreground=[('disabled', '#A0AEC0')])

style.configure("Left.TButton", anchor="w") 
style.configure("Right.TButton", anchor="e") 

# Акцентный стиль для главной кнопки сброса
style.configure("Action.TButton", anchor="w", font=('Segoe UI', 10, 'bold'), padding=6)
#style.map("Action.TButton", background=[('active', '#2980B9')])

# Настройка красивого прогресс-бара
style.configure("TProgressbar", thickness=20, background="#2ECC71", troughcolor="#E2E8F0", bordercolor=BG_COLOR)
# --- КОНЕЦ БЛОКА ВИЗУАЛЬНОГО ОФОРМЛЕНИЯ ---

# Решение 1: Для заголовка окна (кросс-платформенное)
ICON_FILE_PHOTO = "network-switch.gif" 

if os.path.exists(ICON_FILE_PHOTO):
    try:
        icon_photo = PhotoImage(file=ICON_FILE_PHOTO)
        # Устанавливает иконку в заголовке окна
        root.tk.call('wm', 'iconphoto', root._w, icon_photo) 
        print(f"Иконка '{ICON_FILE_PHOTO}' успешно загружена.")
    except Exception as e:
        print(f"Ошибка загрузки PhotoImage: {e}")
else:
    print(f"Файл иконки '{ICON_FILE_PHOTO}' не найден.")


# Решение 2: Для панели задач Windows (требует .ico)
ICON_FILE_BITMAP = "network-switch.ico" # Убедитесь, что этот файл существует!

if os.path.exists(ICON_FILE_BITMAP):
    try:
        # **ЭТОТ МЕТОД ЛУЧШЕ РАБОТАЕТ ДЛЯ TASKBAR В WINDOWS**
        root.iconbitmap(ICON_FILE_BITMAP) 
        print(f"Иконка Taskbar '{ICON_FILE_BITMAP}' успешно установлена.")
    except Exception as e:
        print(f"Ошибка загрузки .ico: {e}")
else:
    print(f"Файл иконки '{ICON_FILE_BITMAP}' (.ico) не найден. Иконка Taskbar может не измениться.")

root.grid_rowconfigure(0, weight=1)

username, password, is_print_value, is_config_value, saved_printer, auto_loop_value, auto_speed_value, mkt_ip_value, mkt_user_value, mkt_pass_value = load_config()
is_print = BooleanVar(value=is_print_value)
is_config = BooleanVar(value=is_config_value)
var_auto_loop = tk.BooleanVar(value=auto_loop_value)
var_auto_speed = tk.BooleanVar(value=auto_speed_value)

# --- Основной контейнер и панель отладки рядом (справа, скрыта по умолчанию) ---
# Фиксированная ширина 600px — без отладки, с отладкой 1300px
MAIN_WIDTH = 750
DEBUG_DEFAULT_WIDTH = 700  # ширина окна отладки по умолчанию
WINDOW_Y = 500

left_container = ttk.Frame(root, width=MAIN_WIDTH)
left_container.pack(side=LEFT, fill=Y, expand=False)
left_container.pack_propagate(False)  # Запрещаем сжиматься

frame_debug = ttk.Frame(root, padding=[5, 5, 5, 5], width=DEBUG_DEFAULT_WIDTH)  # Панель отладки — появляется справа
frame_debug.pack_propagate(False)

debug_visible = False

root.geometry(f"{MAIN_WIDTH}x{WINDOW_Y}+400+200")

def _on_resize(event):
    """При изменении размера окна — растягиваем debug-панель до правой границы, если она видима."""
    if debug_visible:
        new_width = max(DEBUG_DEFAULT_WIDTH, event.width - MAIN_WIDTH - 10)
        frame_debug.config(width=new_width)
        root.update_idletasks()

root.bind('<Configure>', _on_resize)

def clear_debug_output():
    """Очищает окно отладки (по запросу пользователя)."""
    try:
        debugOutput.delete("1.0", END)
    except NameError:
        pass

def copy_debug_output():
    """Копирует весь текст отладки в буфер обмена."""
    try:
        text = debugOutput.get("1.0", END + "-1c")
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()  # Нужно для обновления буфера обмена
    except NameError:
        pass

def toggle_debug_panel():
    global debug_visible
    if debug_visible:
        frame_debug.pack_forget()
        root.geometry(f"{MAIN_WIDTH}x{WINDOW_Y}+400+200")
        btnDebugToggle.config(text="Отладка >>")
        debug_visible = False
    else:
        frame_debug.config(width=DEBUG_DEFAULT_WIDTH)
        frame_debug.pack(side=RIGHT, fill=BOTH, expand=False)
        root.geometry(f"{MAIN_WIDTH + DEBUG_DEFAULT_WIDTH}x{WINDOW_Y}+400+200")
        btnDebugToggle.config(text="<< Скрыть отладку")
        debug_visible = True

# Заголовок программы + кнопка отладки в одной строке
header_frame = ttk.Frame(left_container)
header_frame.pack(anchor=N, fill=X)

# Стилизованный заголовок (Label из классического Tk)
mainlabel = Label(header_frame, text="Сброс коммутаторов и печать наклеек v1.08043", 
                  font=('Segoe UI', 11, 'bold'), bg=BG_COLOR, fg="#2980B9", pady=2) 
mainlabel.pack(anchor=N, side=LEFT)

btnDebugToggle = ttk.Button(header_frame, text="Отладка >>", command=toggle_debug_panel, style="Left.TButton")
btnDebugToggle.pack(anchor=NE, side=RIGHT, padx=10, pady=2)


# Рамки теперь без черной окантовки, разделены отступами (воздухом)
frame1 = ttk.Frame(left_container, padding=[5, 2, 5, 2])
frame1.pack(anchor=NW, fill=BOTH, padx=10, pady=2)
frame1.grid_columnconfigure(0, weight=1, uniform="equal") # Левая колонка строго равна правой
frame1.grid_columnconfigure(1, weight=0)                  # Центр (кнопка и разделитель)
frame1.grid_columnconfigure(2, weight=1, uniform="equal") # Правая колонка строго равна левой

frame2 = ttk.Frame(left_container, padding=[5, 2, 5, 2])
frame2.pack(anchor=W, fill=BOTH , padx=10, pady=2)
frame2.grid_columnconfigure(0, weight=1)
frame2.grid_columnconfigure(1, weight=1)
frame2.grid_columnconfigure(2, weight=1)
frame2.grid_columnconfigure(3, weight=1)

frame3 = ttk.Frame(left_container, padding=[5, 2, 5, 2])
frame3.pack(anchor=W, fill=BOTH , padx=10, pady=2)

frame4 = ttk.Frame(left_container, padding=[5, 2, 5, 2])
frame4.pack(anchor=W, fill=BOTH , padx=10, pady=2)

# --- Фрейм Mikrotik ---
frame_mikrotik = ttk.LabelFrame(left_container, text="Mikrotik (подключение по API, порт 8728)", padding="10")
frame_mikrotik.pack(fill='x', padx=10, pady=(2,2))

mkt_ip_var = StringVar(value=mkt_ip_value)
mkt_user_var = StringVar(value=mkt_user_value)
mkt_pass_var = StringVar(value=mkt_pass_value)

ttk.Label(frame_mikrotik, text="IP:").grid(row=0, column=0, sticky='w', padx=2, pady=2)
ttk.Entry(frame_mikrotik, textvariable=mkt_ip_var, width=16).grid(row=0, column=1, sticky='w', padx=2, pady=2)
ttk.Label(frame_mikrotik, text="Логин:").grid(row=0, column=2, sticky='w', padx=2, pady=2)
ttk.Entry(frame_mikrotik, textvariable=mkt_user_var, width=14).grid(row=0, column=3, sticky='w', padx=2, pady=2)
ttk.Label(frame_mikrotik, text="Пароль:").grid(row=0, column=4, sticky='w', padx=2, pady=2)
ttk.Entry(frame_mikrotik, textvariable=mkt_pass_var, show="*", width=14).grid(row=0, column=5, sticky='w', padx=2, pady=2)

btnPrintMikrotik = ttk.Button(frame_mikrotik, text="Печать наклейки Mikrotik", command=click_btnPrintMikrotik)
btnPrintMikrotik.grid(row=1, column=0, columnspan=6, sticky='ew', padx=2, pady=2)


#при нажатии любой кнопки вызываем функцию проверки
root.bind('<KeyPress>', on_keypress)


# Checkbutton для опции "Печатать наклейку после сброса" — под комбобоксом выбора принтера
enabled_checkbutton = ttk.Checkbutton(frame1, text="Печатать наклейку после сброса", variable=is_print)
enabled_checkbutton.grid(row=4, column=2, columnspan=2, sticky="w", padx=5, pady=2)
ToolTip(enabled_checkbutton, "Наклейка с QR-кодом, датой и моделью")

config_checkbutton = ttk.Checkbutton(frame2, text="Конфиг после сброса (Qtech/Snr)", variable=is_config)
config_checkbutton.grid(row=0, column=2, columnspan=2, sticky="w", padx=15, pady=2)
# Добавление подсказки к чекбоксу
ToolTip(config_checkbutton, "Для кнопок сброса залить конфиг из файлов config.txt с корня программы")

chkAutoLoop = ttk.Checkbutton(frame2, text="Автозапуск цикла", variable=var_auto_loop)
chkAutoLoop.grid(row=1, column=2, columnspan=2, sticky="w", padx=15, pady=2)

# Автоопределение скорости COM — под радиокнопками выбора скорости
chkAutoSpeed = ttk.Checkbutton(frame1, text="Автоопределение скорости COM", variable=var_auto_speed)
chkAutoSpeed.grid(row=4, column=0, sticky="w", padx=5, pady=2)
ToolTip(chkAutoSpeed, "При сбросе: если вывод порта нечитаем — порт переподключится\nна другой скорости (9600<->115200) автоматически")

# Получение списка COM-портов
comports = serial_ports()
comport = StringVar(value=comports[0] if len(comports) > 0 else "")
comport.set(comports[0] if len(comports) > 0 else "")
comspeed = IntVar(value=9600)


# Combobox для COM-порта
comport_cb = ttk.Combobox(frame1, textvariable=comport, values=comports, state="readonly", font=app_font)
comport_cb.grid(row=0, column=0, columnspan=1, sticky="ew", padx=5, pady=2)
# Настройки скорости (левая колонка)
speed_header = ttk.Label(frame1, text=f"Скорость: {comspeed.get()}", font=('Segoe UI', 9, 'bold'))
speed_header.grid(row=1, column=0, sticky="w", padx=5, pady=(5,0))

radio_btn9600 = ttk.Radiobutton(frame1, text="9600", value=9600, variable=comspeed, command=select_speed)
radio_btn9600.grid(row=2, column=0, sticky="w", padx=5, pady=2)
radio_btn115200 = ttk.Radiobutton(frame1, text="115200", value=115200, variable=comspeed, command=select_speed)
radio_btn115200.grid(row=3, column=0, sticky="w", padx=5, pady=2)



# Получаем список принтеров
printer_list = CPrinter().get_available_printers()
selected_printer = StringVar(value=saved_printer if saved_printer in printer_list else (printer_list[0] if printer_list else ""))

# Label и Combobox для выбора принтера
printer_label = ttk.Label(frame1, text="Принтер:", font=('Segoe UI', 9, 'bold'))
printer_label.grid(row=1, column=2, columnspan=2, sticky="w", padx=5, pady=(5,0))

printer_cb = ttk.Combobox(frame1, textvariable=selected_printer, values=printer_list, state="readonly", font=app_font)
printer_cb.grid(row=2, column=2, columnspan=2, rowspan=2, sticky="ew", padx=5, pady=2)
ToolTip(printer_cb, "Сохраняется при закрытии")

# Добавляем вертикальный разделитель
separator = ttk.Separator(frame1, orient='vertical')
separator.grid(row=1, column=1, rowspan=4, sticky="ns", padx=1, pady=1)

btnCheckCom = ttk.Button(frame1, text="Перечитать порты", command=click_btnCheckCom, style="Left.TButton")
btnCheckCom.grid(sticky="ew", row=0, column=1, padx=5, pady=2)

btnSbros_text = StringVar(value="1. Сброс к заводским")
# Применяем акцентный стиль "Action.TButton" для главной кнопки
btnSbros = ttk.Button(frame2, textvariable=btnSbros_text, command=click_btnSbros, style="Action.TButton") 
btnSbros.grid(sticky=EW, row=1, column=0, columnspan=2, padx=2, pady=2)


progress = IntVar(value=0) # прогрессбар
progressbar =  ttk.Progressbar(frame2, orient="horizontal", variable=progress)
progressbar.grid(sticky="ew", row=4, column=0, columnspan=4, padx=2, pady=2)
# Текст отсчёта поверх прогресс-бара
progressbar_label = ttk.Label(frame2, text="", font=('Segoe UI', 11, 'bold')) 
progressbar_label.grid(row=5, column=0, columnspan=4, pady=(5, 10))

btnPrintStickAuto = ttk.Button(frame3, text="Печать QR подключенного коммутатора", command=click_btnPrintStickAuto)
btnPrintStickAuto.grid(sticky=NW, row=1, column=1, columnspan=3, padx=2, pady=2)
ToolTip(btnPrintStickAuto, "Определит производителя уже подключенного (залогиненного) коммутатора и напечатает наклейку")

lblUserpass = ttk.Label(frame3, text="Использовать эти данные, если не подойдут стандартные:", foreground="#7F8C8D")
lblUserpass.grid(sticky=W, row=2, column=1, columnspan=4, pady=(5,2))
ToolTip(lblUserpass, "Сохраняется при закрытии")

lblUser = ttk.Label(frame3, text="Login:")
lblUser.grid(sticky=NW, row=3, column=1, columnspan=1, padx=2, pady=2)
entryUser = ttk.Entry(frame3, name='loginEnt', font=app_font)
entryUser.grid(sticky=NW, row=3, column=2, columnspan=2, padx=2, pady=2)

lblPass = ttk.Label(frame3, text="Password:")
lblPass.grid(sticky=NW, row=4, column=1, columnspan=1, padx=2, pady=2)
entryPass = ttk.Entry(frame3, name='passwordEnt', show="*", font=app_font)
entryPass.grid(sticky=NW, row=4, column=2, columnspan=2, padx=2, pady=2)


if username and password:# Заполняем поля сохранёнными данными
    entryUser.insert(0, username)
    entryPass.insert(0, password)

frame4.grid_columnconfigure(1, weight=1)  # Делаем колонку растяжимой

lblSticks = ttk.Label(frame4, text="Напечатать наклейки без подключения к коммутатору:", font=('Segoe UI', 9, 'bold'))
lblSticks.grid(sticky=EW, row=0, column=1, columnspan=2, padx=2, pady=2)

lblMac = ttk.Label(frame4, text="MAC", font=('Segoe UI', 9, 'bold'))
lblMac.grid(sticky=EW, row=1, column=1, padx=2, pady=2)
entryMac = ttk.Entry(frame4, name='edmac', font=app_font)
entryMac.grid(sticky=EW, row=2, column=1, columnspan=1, padx=2, pady=2)

lblModel = ttk.Label(frame4, text="Model", font=('Segoe UI', 9, 'bold'))
lblModel.grid(sticky=EW, row=1, column=2, padx=2, pady=2)
entryModel = ttk.Entry(frame4, name='edmodel', font=app_font)
entryModel.grid(sticky=EW, row=2, column=2, columnspan=1, padx=2, pady=2)

lblSerial = ttk.Label(frame4, text="Serial", font=('Segoe UI', 9, 'bold'))
lblSerial.grid(sticky=EW, row=1, column=3, padx=2, pady=2)  
entrySerial = ttk.Entry(frame4, name='edserial', font=app_font)
entrySerial.grid(sticky=EW, row=2, column=3, columnspan=1, padx=2, pady=2)

btnPrintStickData = ttk.Button(frame4, text="↑ Наклейка из данных", command=click_btnPrintStickData) 
btnPrintStickData.grid(sticky=EW, row=3, column=1, padx=2, pady=(5,2))

btnPrintRemont = ttk.Button(frame4, text="R. Наклейка Ремонт", command=click_btnPrintRemont)                                                                                     
btnPrintRemont.grid(sticky=EW, row=3, column=2, columnspan=1, padx=5, pady=(5,2))

btnPrintSpisanie = ttk.Button(frame4, text="X. Наклейка Списание", command=click_btnPrintSpisanie)                                                                                     
btnPrintSpisanie.grid(sticky=EW, row=3, column=3, columnspan=1, padx=5, pady=(5,2))

# Основной лог — светлый, "не терминальный" фон: просто показывает, какая процедура сейчас идёт

# --- Панель отладки (справа, появляется по кнопке "Отладка >>") ---
debug_header = ttk.Label(frame_debug, text="Отладка: сырой обмен с COM-портом", font=('Segoe UI', 10, 'bold'))
debug_header.pack(anchor=NW, padx=5, pady=(0,5))

# Кнопки очистки и копирования отладки — по разным сторонам
btn_debug_frame = ttk.Frame(frame_debug)
btn_debug_frame.pack(fill='x', padx=5, pady=(0,5))

btnClearDebug = ttk.Button(btn_debug_frame, text="🗑 Очистить", command=clear_debug_output, style="Right.TButton")
btnClearDebug.pack(side='left')

btnCopyDebug = ttk.Button(btn_debug_frame, text="📋 Копировать", command=copy_debug_output, style="Right.TButton")
btnCopyDebug.pack(side='right')

debugOutput = ScrolledText(frame_debug, width=70, height=40, font=('Consolas', 9),
                            bg='#101418', fg='#D4D4D4', insertbackground='white',
                            relief='flat', borderwidth=0, padx=8, pady=8)
debugOutput.pack(fill=BOTH, expand=True, padx=5, pady=(0,5))
debugOutput.tag_config("output", foreground="#4EC9B0")  # Output — то, что реально пришло от коммутатора
debugOutput.tag_config("input", foreground="#DCDCAA")   # Input — то, что мы отправили в порт
debugOutput.tag_config("proc", foreground="#9CDCFE")    # Наши процедурные сообщения (как в обычном логе)

root.after(150, _drain_debug_queue)  # Запускаем периодический перенос сырых данных в окно отладки

disable_buttons()  #Блочим все почти кнопки в конце отрисовки интерфейса, пока не проверим что порты доступны
btnPrintRemont.config(state='normal') #Эти как раз не блочим
btnPrintStickData.config(state='normal') #Эти как раз не блочим
btnPrintSpisanie.config(state='normal') #Эти как раз не блочим
btnPrintMikrotik.config(state='normal') #Mikrotik работает по API, не зависит от COM-порта

#Проверяем доступность портов в функции. Если всё ок, возвращаем активность кнопкам
if check_com_ports():
    btnSbros.config(state='normal')


# Привязываем обработчик к событию закрытия окна
root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop() #Для отображения окна и взаимодействия с пользователем

####################################################################################
#                    Конец    Графического интерфейса                              #
####################################################################################



#---------------------------- main -----------------------------------------------------------------------------




exit()