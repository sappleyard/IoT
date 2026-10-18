idf_component_register(SRCS "main.c" "battery.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES protocol mic radio serial_link esp_timer esp_adc)
