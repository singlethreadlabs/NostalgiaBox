package com.nostalgiabox.tv

data class ProgramInfo(val title: String)

data class ChannelInfo(
    val number: Int,
    val name: String,
    val program: ProgramInfo,
)

data class ChannelLineup(
    val startChannel: Int,
    val channels: List<ChannelInfo>,
)

data class PlaybackSession(
    val id: String,
    val deliveryMode: String,
    val mediaUrl: String,
    val hlsUrl: String?,
    val initialOffsetSeconds: Double,
    val program: ProgramInfo,
)
