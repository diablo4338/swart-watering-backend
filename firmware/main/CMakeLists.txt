idf_component_register(SRCS "main.c" "app_watchdog.c" "hx711.c" "watering.c" "web.c" "wifi_sta.c"
                       INCLUDE_DIRS "."
                       REQUIRES
                           esp_driver_gpio
                           esp_event
                           esp_http_client
                           esp_http_server
                           esp_netif
                           esp_system
                           esp_wifi
                           nvs_flash)
