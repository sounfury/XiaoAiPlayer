# language: zh-CN
功能: 电脑上的 Spotify 没开时的表现
  作为 小爱音箱的主人
  我想要 即使电脑上的 Spotify 没打开，点歌也能直接播放
  但是 只是说暂停时不要平白把 Spotify 打开

  背景:
    假如 小爱播放助手服务正在运行
    而且 电脑上的 Spotify 没有运行

  场景: Spotify 没开时说暂停，不会打开 Spotify
    当 我对小爱说 "暂停"
    那么 电脑上的 Spotify 不应该被打开

  场景: Spotify 没开时点歌，会自动打开 Spotify 并播放
    当 我对小爱说 "播放周杰伦的晴天"
    那么 电脑上的 Spotify 应该被自动打开
    而且 电脑上的 Spotify 应该在播放 "晴天"，歌手是 "Jay Chou"
    而且 小爱音箱不应该播放它自己的音乐
