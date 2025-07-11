from opendbc.car import Bus, structs
from opendbc.can.parser import CANParser
from opendbc.can.can_define import CANDefine
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.interfaces import CarStateBase
from opendbc.car.byd.values import DBC, CANBUS

# Multiplier between GPS ground speed to the meter cluster's displayed speed
HUD_MULTIPLIER = 1.068

class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    print(CP.carFingerprint)
    can_define = CANDefine(DBC[CP.carFingerprint][Bus.pt])

    self.shifter_values = can_define.dv["DRIVE_STATE"]['GEAR']
    self.set_distance_values = can_define.dv['ACC_HUD_ADAS']['SET_DISTANCE']

    self.prev_angle = 0
    self.lss_state = 0
    self.lss_alert = 0
    self.tsr = 0
    self.ahb = 0
    self.passthrough = 0
    self.lka_on = 0
    self.HMA = 0
    self.lkas_rdy_btn = False
    self.lkas_faulted = False

  def update(self, can_parsers) -> structs.CarState:
    cp = can_parsers[Bus.pt]
    cp_cam = can_parsers[Bus.cam]
    ret = structs.CarState()

    self.tsr = cp_cam.vl["LKAS_HUD_ADAS"]['TSR']
    self.lka_on = cp_cam.vl["LKAS_HUD_ADAS"]['STEER_ACTIVE_ACTIVE_LOW']
    self.lkas_rdy_btn = cp.vl["PCM_BUTTONS"]['LKAS_ON_BTN']
    self.abh = cp_cam.vl["LKAS_HUD_ADAS"]['SET_ME_XFF']
    self.passthrough = cp_cam.vl["LKAS_HUD_ADAS"]['SET_ME_X5F']
    self.HMA = cp_cam.vl["LKAS_HUD_ADAS"]['HMA']
    self.lkas_healthy = cp_cam.vl["STEERING_MODULE_ADAS"]['EPS_OK']

    # EV irrelevant messages
    ret.brakeHoldActive = False

    ret.wheelSpeeds = self.get_wheel_speeds(
      cp.vl["WHEEL_SPEED"]['WHEELSPEED_FL'],
      cp.vl["WHEEL_SPEED"]['WHEELSPEED_FL'],
      cp.vl["WHEEL_SPEED"]['WHEELSPEED_BL'],
      cp.vl["WHEEL_SPEED"]['WHEELSPEED_BL'],
    )
    ret.vEgoRaw = (ret.wheelSpeeds.rl + ret.wheelSpeeds.fl) / 2.0

    # unfiltered speed from CAN sensors
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    ret.vEgoCluster = ret.vEgo
    ret.standstill = ret.vEgoRaw < 0.01

    # safety checks to engage
    can_gear = int(cp.vl["DRIVE_STATE"]['GEAR'])

    ret.doorOpen = any([cp.vl["METER_CLUSTER"]['BACK_LEFT_DOOR'],
                     cp.vl["METER_CLUSTER"]['FRONT_LEFT_DOOR'],
                     cp.vl["METER_CLUSTER"]['BACK_RIGHT_DOOR'],
                     cp.vl["METER_CLUSTER"]['FRONT_RIGHT_DOOR']])

    ret.seatbeltUnlatched = cp.vl["METER_CLUSTER"]['SEATBELT_DRIVER'] == 0
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(can_gear, None))

    # gas pedal
    ret.gas = cp.vl["PEDAL"]['GAS_PEDAL']
    ret.gasPressed = ret.gas >= 0.01

    # brake pedal
    ret.brake = cp.vl["PEDAL"]['BRAKE_PEDAL']
    ret.brakePressed = bool(cp.vl["DRIVE_STATE"]["BRAKE_PRESSED"]) or ret.brake > 0.01

    # steer
    ret.steeringAngleDeg = cp.vl["STEER_MODULE_2"]['STEER_ANGLE_2']
    steer_dir = 1 if (ret.steeringAngleDeg - self.prev_angle >= 0) else -1
    self.prev_angle = ret.steeringAngleDeg
    ret.steeringTorque = cp.vl["STEERING_TORQUE"]['MAIN_TORQUE']
    ret.steeringTorqueEps = cp.vl["STEER_MODULE_2"]['DRIVER_EPS_TORQUE'] * steer_dir
    ret.steeringPressed = bool(abs(ret.steeringTorqueEps) > 6)

    # TODO: get the real value
    ret.stockAeb = False
    ret.stockFcw = False
    ret.cruiseState.available = any([cp_cam.vl["ACC_HUD_ADAS"]["ACC_ON1"], cp_cam.vl["ACC_HUD_ADAS"]["ACC_ON2"]])

    # byd speedCluster will follow wheelspeed if cruiseState is not available
    if ret.cruiseState.available:
      ret.cruiseState.speedCluster = max(int(cp_cam.vl["ACC_HUD_ADAS"]['SET_SPEED']), 30) * CV.KPH_TO_MS
    else:
      ret.cruiseState.speedCluster = 0

    ret.cruiseState.speed = ret.cruiseState.speedCluster / HUD_MULTIPLIER
    ret.cruiseState.standstill = bool(cp_cam.vl["ACC_CMD"]["STANDSTILL_STATE"])
    ret.cruiseState.nonAdaptive = False

    ret.cruiseState.enabled = bool(cp_cam.vl["ACC_CMD"]["ACC_CONTROLLABLE_AND_ON"])

    # button presses
    ret.leftBlinker = bool(cp.vl["STALKS"]["LEFT_BLINKER"])
    ret.rightBlinker = bool(cp.vl["STALKS"]["RIGHT_BLINKER"])
    ret.genericToggle = bool(cp.vl["STALKS"]["GENERIC_TOGGLE"])
    ret.espDisabled = False

    # blindspot sensors
    if self.CP.enableBsm:
      # used for lane change so its okay for the chime to work on both side.
      ret.leftBlindspot = bool(cp.vl["BSM"]["LEFT_APPROACH"])
      ret.rightBlindspot = bool(cp.vl["BSM"]["RIGHT_APPROACH"])

    self.lss_state = cp_cam.vl["LKAS_HUD_ADAS"]["LSS_STATE"]
    self.lss_alert = cp_cam.vl["LKAS_HUD_ADAS"]["SETTINGS"]
    return ret


  @staticmethod
  def get_can_parsers(CP):
    pt_signals = [
      ("DRIVE_STATE", 50),
      ("WHEEL_SPEED", 50),
      ("PEDAL", 50),
      ("METER_CLUSTER", 20),
      ("STEER_MODULE_2", 100),
      ("STEERING_TORQUE", 50),
      ("STALKS", 0),
      ("BSM", 20),
      ("PCM_BUTTONS", 0),
    ]

    cam_signals = [
      ("ACC_HUD_ADAS", 50),
      ("ACC_CMD", 50),
      ("LKAS_HUD_ADAS", 50),
      ("STEERING_MODULE_ADAS", 50),
    ]

    return {
      Bus.pt: CANParser(DBC[CP.carFingerprint][Bus.pt], pt_signals, CANBUS.main_bus),
      Bus.cam: CANParser(DBC[CP.carFingerprint][Bus.pt], cam_signals, CANBUS.cam_bus),
    }
