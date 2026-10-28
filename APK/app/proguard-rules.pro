# Add project specific ProGuard rules here.
# By default, the flags in this file are appended to flags specified
# in getDefaultProguardFile("proguard-android-optimize.txt").

# Paho MQTT Client
-keep class org.eclipse.paho.client.mqttv3.** { *; }
-dontwarn org.eclipse.paho.client.mqttv3.**
