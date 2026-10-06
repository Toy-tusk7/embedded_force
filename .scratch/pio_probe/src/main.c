#include "stm32f4xx_hal.h"

int main(void) {
    HAL_Init();
    while (1) {
        HAL_Delay(100);
    }
}
