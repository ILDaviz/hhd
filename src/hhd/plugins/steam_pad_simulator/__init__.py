import logging
import threading
import time # NUOVO: Importiamo 'time' per la pausa della vibrazione
from typing import Sequence

# Importa le librerie necessarie
from evdev import UInput, ecodes as e, InputDevice, AbsInfo

from hhd.controller import Event
from hhd.plugins import Config, Context, HHDPlugin, load_relative_yaml

logger = logging.getLogger(__name__)

# Dati identificativi del controller Steam Deck
STEAM_DECK_VID = 0x28de
STEAM_DECK_PID = 0x1142

# NUOVO: Dimensioni dello schermo per il debug. In futuro potrebbero essere lette dinamicamente.
SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080


class SteamPadSimulatorPlugin(HHDPlugin):
    def __init__(self) -> None:
        # Nome e priorità del plugin
        self.name = "steam_pad_simulator"
        self.priority = 10
        self.log = "SPS"

        # Stato del plugin
        self.enabled = False
        self.touch_device_path = None
        self.sensitivity = 1.0
        # NUOVO: Variabili di stato per le nuove opzioni
        self.enable_haptics = True
        self.show_debug_borders = False

        # Oggetti di input/output
        self.virtual_controller = None
        self.touch_device = None
        self.input_thread = None
        self.stop_thread = threading.Event()

    def open(self, emit, context: Context):
        self.emit = emit

    def settings(self):
        # Carica il file settings.yml che ora avrà la chiave principale "steam_pad_simulator"
        return load_relative_yaml("settings.yml")

    def update(self, conf: Config):
        # Leggi la configurazione usando il nuovo nome
        config_section = conf.get("steam_pad_simulator", {})
        new_enabled = config_section.get("enable", False)
        new_device_path = config_section.get("device_path", "/dev/input/event5")
        new_sensitivity = config_section.get("sensitivity", 1.0)
        # NUOVO: Leggiamo le nuove opzioni dalla configurazione
        self.enable_haptics = config_section.get("enable_haptics", True)
        self.show_debug_borders = config_section.get("show_debug_borders", False)

        # Logica per avviare/fermare il thread
        if (self.enabled != new_enabled or
            self.touch_device_path != new_device_path):

            self.enabled = new_enabled
            self.touch_device_path = new_device_path

            if self.input_thread:
                self.stop_thread.set()
                self.input_thread.join()
                self.stop_thread.clear()

            if self.enabled:
                logger.info(f"Avvio di Steam Pad Simulator su '{self.touch_device_path}'")
                self.input_thread = threading.Thread(target=self._input_thread_loop)
                self.input_thread.start()

        self.sensitivity = new_sensitivity

    def close(self):
        logger.info("Richiesta di chiusura di Steam Pad Simulator.")
        if self.input_thread:
            self.stop_thread.set()
            self.input_thread.join()

    def _input_thread_loop(self):
        try:
            capabilities = {
                e.EV_ABS: [
                    (e.ABS_X, AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128)),
                    (e.ABS_Y, AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128)),
                ],
                e.EV_REL: [e.REL_X, e.REL_Y],
                e.EV_KEY: [e.BTN_A, e.BTN_B, e.BTN_LEFT],
                e.EV_FF: [e.FF_RUMBLE]
            }
            self.virtual_controller = UInput(
                vendor=STEAM_DECK_VID,
                product=STEAM_DECK_PID,
                name="HHD Steam Pad Simulator",
                capabilities=capabilities
            )
            self.touch_device = InputDevice(self.touch_device_path)
            logger.info("Thread di input avviato e controller virtuale creato.")
        except Exception as err:
            logger.error(f"Errore nell'inizializzazione del thread di input: {err}")
            return

        # MODIFICATO: Logica del loop espansa per le nuove funzionalità
        state = {'touching': None, 'x': 0, 'y': 0, 'last_x': 0, 'last_y': 0}

        for event in self.touch_device.read_loop():
            if self.stop_thread.is_set():
                break

            # Colleziona le coordinate
            if event.type == e.EV_ABS:
                if event.code == e.ABS_X: state['x'] = event.value
                elif event.code == e.ABS_Y: state['y'] = event.value

            # Gestisce l'inizio e la fine del tocco
            if event.type == e.EV_KEY and event.code == e.BTN_TOUCH:
                side = 'left' if state['x'] < SCREEN_WIDTH / 2 else 'right'

                if event.value == 1 and not state['touching']: # Inizio Tocco
                    state['touching'] = side
                    state['last_x'], state['last_y'] = state['x'], state['y']

                    # NUOVO: Logica per la vibrazione
                    if self.enable_haptics:
                        self.emit.inject({"type": "rumble", "code": "main", "strong_magnitude": 0.6, "weak_magnitude": 0.6})
                        time.sleep(0.05) # Durata della vibrazione
                        self.emit.inject({"type": "rumble", "code": "main", "strong_magnitude": 0, "weak_magnitude": 0})

                    # NUOVO: Logica per il bordo di debug
                    if self.show_debug_borders:
                        border_rect = {
                            "id": f"sps_debug_{side}",
                            "type": "overlay",
                            "x": 0 if side == 'left' else SCREEN_WIDTH / 2,
                            "y": 0,
                            "width": SCREEN_WIDTH / 2,
                            "height": SCREEN_HEIGHT,
                            "color": (255, 0, 0, 100), # Rosso semitrasparente
                            "border_size": 5,
                        }
                        self.emit.inject(border_rect)

                elif event.value == 0 and state['touching']: # Fine Tocco
                    # NUOVO: Rimuovi il bordo di debug
                    if self.show_debug_borders:
                        self.emit.inject({"type": "overlay", "id": f"sps_debug_{state['touching']}", "remove": True})
                    state['touching'] = None

            # Gestisce il movimento
            if event.type == e.EV_SYN and state['touching']:
                delta_x = int((state['x'] - state['last_x']) * self.sensitivity)
                delta_y = int((state['y'] - state['last_y']) * self.sensitivity)

                if delta_x != 0: self.virtual_controller.write(e.EV_REL, e.REL_X, delta_x)
                if delta_y != 0: self.virtual_controller.write(e.EV_REL, e.REL_Y, delta_y)

                self.virtual_controller.syn()
                state['last_x'], state['last_y'] = state['x'], state['y']


        if self.virtual_controller:
            self.virtual_controller.close()
        logger.info("Thread di input fermato e controller virtuale chiuso.")


def autodetect(existing: Sequence[HHDPlugin]) -> Sequence[HHDPlugin]:
    if len(existing):
        return existing
    return [SteamPadSimulatorPlugin()]