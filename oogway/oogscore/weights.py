WIN_BONUS    = 3.0
LOSS_PENALTY = -1.0
CLUTCH_BONUS = 2.0

GRADE_THRESHOLDS = [(85, "S"), (72, "A"), (58, "B"), (42, "C"), (0, "D")]

ROLE_WEIGHTS: dict[str, dict[str, float]] = {
    "TOP":     {"KDA":.15,"KP":.10,"DMG":.22,"ECO":.13,"OBJ":.08,"VIS":.03,"UTL":.02,"LANE":.17,"CC":.10},
    "JUNGLE":  {"KDA":.12,"KP":.15,"DMG":.16,"ECO":.08,"OBJ":.25,"VIS":.08,"UTL":.02,"LANE":.00,"CC":.14},
    "MID":     {"KDA":.15,"KP":.12,"DMG":.25,"ECO":.13,"OBJ":.08,"VIS":.04,"UTL":.02,"LANE":.13,"CC":.08},
    "ADC":     {"KDA":.15,"KP":.12,"DMG":.30,"ECO":.18,"OBJ":.08,"VIS":.03,"UTL":.02,"LANE":.12,"CC":.00},
    "SUPPORT": {"KDA":.10,"KP":.22,"DMG":.05,"ECO":.00,"OBJ":.12,"VIS":.25,"UTL":.20,"LANE":.00,"CC":.06},
}

ROLE_CONFIDENCE_THRESHOLD = 100
CHAMPION_CONFIDENCE_THRESHOLD = 50
CHAMPION_FULL_CONFIDENCE_N = 200

def grade_from_score(score: float) -> str:
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "D"
