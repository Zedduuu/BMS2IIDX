"""界面上的「帮助」「关于」两页文案（HTML）。

单独放一个模块：文案动得比代码勤，改这里不用碰逻辑。
"""

# ---------------------------------------------------------------------- 帮助
HELP_PAGE = """
<style>
  h2 { margin-top: 18px; margin-bottom: 6px; }
  h3 { margin-top: 14px; margin-bottom: 4px; }
  p, li { line-height: 150%; }
  code { background: rgba(127,127,127,0.16); padding: 1px 4px; }
  .tip { color: #2f7d32; }
  .warn { color: #b35c00; }
  table { border-collapse: collapse; margin: 6px 0 10px 0; }
  td, th { border: 1px solid rgba(127,127,127,0.45); padding: 4px 8px; vertical-align: top; }
  th { background: rgba(127,127,127,0.12); text-align: left; }
</style>

<h2>目录</h2>
<ul>
<li><a href="#quick">一、三分钟上手</a></li>
<li><a href="#flow">二、它到底做了什么（基本逻辑）</a></li>
<li><a href="#words">三、名词解释</a></li>
<li><a href="#detail">四、更细的说明</a></li>
</ul>

<h2 id="quick">一、三分钟上手</h2>

<h3>1. 先告诉程序游戏在哪</h3>
<p>「转换」页最上面的<b>目标游戏目录</b>，点「选择」，选到 <code>…\\contents</code> 这一层
（里面有 <code>data</code> 和 <code>data_mods</code> 两个文件夹的那个）。</p>
<p>选完会自动检测，下面「游戏环境」会写清楚：装了哪种 omnimix、曲库文件叫什么、
槽位多少、下一个空号是多少、补丁层（ifs_hook）有没有加载。</p>
<p class="warn">游戏目录只需要选一次，之后会记住。</p>

<h3>2. 选一首 BMS，把它加进队列</h3>
<ol>
<li>点「选择」挑一个 BMS 歌曲文件夹（也可以直接把文件夹拖进窗口），再点「读取」。</li>
<li>左边列出这个文件夹里所有谱面。勾选要导入的几个。</li>
<li>把勾选的谱面拖到右边对应难度槽（B / N / H / A / L）。SP 和 DP 分开。</li>
<li>核对右边<b>曲名 / 曲风 / 作曲人</b>；「歌曲 ID」留空就会自动分配。</li>
<li>看一眼<b>副标题</b>（默认取谱面的 #SUBARTIST，也就是谱师名；可以改、可以清空）。</li>
<li>按需要勾选<b>试听 / 封面 / BGA / 坏判层</b>，选<b>放入 IIDX 文件夹</b>。</li>
<li>点「添加到队列」，程序会跳到「队列」页。</li>
</ol>
<p class="tip">想连着做很多首？重复第 2 步即可，队列会排起来。</p>

<h3>3. 转换，然后部署</h3>
<ol>
<li>在「队列」页点「开始转换」。这一步<b>只写暂存区</b>（程序目录下的 <code>out\\staging</code>），
游戏目录一个字节都不会动。</li>
<li>进度条走完、状态显示「已转换」，确认没问题后点「部署到游戏」。</li>
<li>部署前会自动给曲库/XML 拍一份备份，然后把文件复制进游戏目录。</li>
<li>重启游戏，在对应的 IIDX 文件夹里找这首歌。</li>
</ol>

<h3>4. 后悔了怎么办</h3>
<ul>
<li>只是部署错了：「备份与恢复」页选部署前那份备份，点「恢复选中」。</li>
<li>想把某首歌删掉：「IIDX 曲库与资源」页右键 → 选删除方式（建议「从曲库移除 + 文件移到回收区」）。</li>
<li>删错了：「回收区…」里可以还原。</li>
</ul>

<h2 id="flow">二、它到底做了什么（基本逻辑）</h2>

<h3>转换（只写暂存区）</h3>
<p>一首歌的转换顺序是<b>串行</b>的，不会边转音频边合成画面：</p>
<ol>
<li><b>解析谱面</b>：读 BMS 的头信息和音符，算出时长、物量、BPM 变化。</li>
<li><b>键音转码</b>：每个采样文件（wav/ogg/mp3/flac…）单独转成 IIDX 用的
<code>MS-ADPCM 44.1kHz</code>，再打包进这首歌的 2DX 容器。<b>只转格式，不改音量</b>。</li>
<li><b>谱面写入</b>：把 BMS 的音符换算成 IIDX 的 tick 时间轴，写成 <code>.1</code> 文件。</li>
<li><b>曲库记录</b>：在暂存区的 <code>music_omni.bin</code> 里新增一条记录（曲名、曲风、作者、等级、封面…）。</li>
<li><b>BGA</b>（如果勾了）：按原曲的分层规则合成影片。</li>
<li><b>附属数据</b>：封面缩略图、试听片段、坏判层、本机库记录。</li>
</ol>

<h3>为什么分「暂存」和「部署」</h3>
<p>转换是一次尝试，可能不符合预期（难度选错、画面不满意、ID 想改）。暂存区就是草稿纸：
可以反复转换、反复覆盖，游戏目录始终干净。只有你点「部署到游戏」才真正写进去，
而且写之前会自动备份。</p>

<h3>部署时写了什么</h3>
<ul>
<li><b>新增</b> <code>data_mods\\omnimix\\sound\\&lt;ID&gt;\\</code>（谱面、键音、试听）</li>
<li><b>新增</b> <code>…\\movie\\&lt;ID&gt;.mp4</code>（32 代以后）或 <code>.wmv</code>（26 代以前）</li>
<li><b>新增</b> <code>…\\graphic\\movie_thumbnail\\&lt;ID&gt;_thum.png</code>（32 代以后才有封面）</li>
<li><b>重写</b> <code>…\\info\\&lt;槽&gt;\\music_omni.bin</code>（曲库，只多一条记录）</li>
<li><b>新增</b> 三个 <code>*.merged.xml</code>（曲名/作者/影片的增量，运行时由 layeredfs 合并）</li>
<li><b>记录</b> <code>bms2iidx_deployed.json</code>（我们装过什么，删歌时要用）</li>
</ul>
<p>它<b>不会</b>碰：游戏 exe/dll、spice2x 配置、存档、其它歌曲的任何文件。</p>

<h3>BMS 谱面库（找歌用）</h3>
<p>第二个标签页把 BMS 根目录<b>建成索引</b>，列表<b>一行 = 一首歌</b>（同一首歌的多个谱面合并成一行，
显示谱面数、最高难度/等级）；选中后下面的「选中项」会列出这首歌的全部谱面，
双击或点「送到『转换』页挑谱面」就带着这个文件夹回到转换页。
索引只读每个谱面的文件头，
几秒钟就能按曲名/艺术家/流派/难度/键数筛。双击结果直接送到「转换」页。</p>
<p>索引是 <code>library\\bms_index.sqlite</code>，删了只会让下次重扫一次。</p>

<h2 id="words">三、名词解释</h2>

<table>
<tr><th>词</th><th>在这里指什么</th></tr>
<tr><td><b>曲库</b></td><td>IIDX 游戏里的<b>选曲列表</b>，就是 <code>music_omni.bin</code> /
<code>music_data.bin</code> 这个文件。歌在不在游戏里，取决于它。</td></tr>
<tr><td><b>资源 / 文件</b></td><td>音源（<code>sound\\</code>）、影片（<code>movie\\</code>）、
封面（<code>graphic\\</code>）。删了曲库记录但留着文件，叫“残留”。</td></tr>
<tr><td><b>本机库</b></td><td><b>本程序自己的</b>记录目录：<code>library\\&lt;安装名&gt;_&lt;哈希&gt;\\</code>，
存每首歌的原始曲库记录、封面、配置。不在游戏目录里。</td></tr>
<tr><td><b>暂存区</b></td><td>程序目录下的 <code>out\\staging</code>。转换产物先放这里，
点部署才复制进游戏。</td></tr>
<tr><td><b>回收区</b></td><td><code>contents</code> 同级的 <code>bms2iidx_trash</code>。
删歌时文件先挪到这里，可以还原；也可以在里面彻底删除。</td></tr>
<tr><td><b>omnimix</b></td><td>王国民的增补包（<code>data_mods\\omnimix</code>），
通过 layeredfs 覆盖到游戏数据上，让老歌回到新版里。</td></tr>
<tr><td><b>layeredfs / ifs_hook</b></td><td>spice2x 的补丁层：游戏读文件时先去
<code>data_mods</code> 里找。它只<b>新增</b>文件，<b>不覆盖</b>游戏本体里已有的同名文件。</td></tr>
<tr><td><b>槽位 / 槽</b></td><td>曲库表格里的一行。每行对应一个歌曲 ID，空行才能放新歌。</td></tr>
<tr><td><b>号段</b></td><td>ID 的千位段。老歌在 1xxx，第 25 代在 25xxx，34 代在 34xxx。
「放入 IIDX 文件夹」就是选号段。</td></tr>
<tr><td><b>键音</b></td><td>BMS 里每个音符对应的那个音频采样。</td></tr>
<tr><td><b>2DX / 2DX9</b></td><td>IIDX 的音频容器格式，里面是 MS-ADPCM。
<code>.2dx</code> 是键音包，<code>_pre.2dx</code> 是切歌试听。</td></tr>
<tr><td><b>BGA</b></td><td>背景动画。<code>#BMP</code> 指向的图/视频，加上
<code>#BGA</code> 事件的层级与切换。</td></tr>
<tr><td><b>图层包（ifs）</b></td><td>IIDX 的图形打包格式，坏判层、选曲立绘都在里面。</td></tr>
<tr><td><b>坏判层（POOR）</b></td><td>盘面下方那层“按错时会闪的图”。这功能是借用现成图形包模板，
把原曲的 miss 图塞进去。</td></tr>
<tr><td><b>难度槽</b></td><td>B(EGINNER) / N(ORMAL) / H(YPER) / A(NOTHER) / L(EGGENDARIA)。
同一首歌的不同难度。</td></tr>
<tr><td><b>SP / DP、7K / 5K</b></td><td>单人或双人、7 键或 5 键。</td></tr>
<tr><td><b>副标题</b></td><td>曲名下面那一行小字。我们用它放<b>谱师署名</b>，
和 Konami 的版权行是同一个位置。</td></tr>
<tr><td><b>字体号（font）</b></td><td>曲名用哪套字库。0 是拉丁字库（没有日文/中文），
1–4 是日文字库。选错会变方块。</td></tr>
<tr><td><b>merged XML</b></td><td>我们生成的小增量文件（曲名/作者/影片），
由 layeredfs 在启动时并进官方 XML，<b>不改原文件</b>。</td></tr>
</table>

<h2 id="detail">四、更细的说明</h2>

<h3>4.1 转换产物（暂存区里长什么样）</h3>
<pre>out\\staging\\data_mods\\omnimix\\
    info\\0\\music_omni.bin          ← 曲库（含新歌）
    info\\0\\music_title_omni.merged.xml 等三个
    sound\\&lt;ID&gt;\\&lt;ID&gt;.1              ← 谱面
    sound\\&lt;ID&gt;\\&lt;ID&gt;.2dx            ← 键音包
    sound\\&lt;ID&gt;\\&lt;ID&gt;_pre.2dx        ← 试听
    movie\\&lt;ID&gt;.mp4 / .wmv
    graphic\\movie_thumbnail\\&lt;ID&gt;_thum.png</pre>
<p>ID 一律 5 位补零（<code>01999</code>），文件名和文件夹名都是。</p>

<h3>4.2 音频怎么处理</h3>
<table>
<tr><th>模式</th><th>做什么</th><th>用在哪</th></tr>
<tr><td>键音模式（默认）</td><td>每个采样单独转成 ADPCM，<b>音量一比特不改</b></td><td>界面目前固定用这个</td></tr>
<tr><td>单流模式</td><td>整曲混成一条流，再把整首 RMS 拉到约 -11 dBFS、软限幅 -1 dBFS</td><td>需要和官方响度对齐时</td></tr>
<tr><td>混合模式</td><td>BGM 进流、音符保留键音</td><td>特殊谱面</td></tr>
</table>
<p>试听（<code>_pre.2dx</code>）是混音后取前 10 秒，每轨乘 0.7 相加、超过满刻度才整体缩小，
末尾 0.2 秒淡出。</p>

<h3>4.3 BGA 规则</h3>
<ul>
<li>原曲是<b>视频底</b>：直接转码，不裁切、不放大、不插值。</li>
<li>原曲是<b>图片底 + 翻转层</b>：按时间轴合成成视频。</li>
<li>纯黑（<code>#000000</code>）按透明处理，保证原曲的“镂空”效果不被破坏。</li>
<li>代次不同容器不同：26 代以前是 <code>.wmv</code>（wmv2），32 代以后是 <code>.mp4</code>。</li>
<li>老代影片覆盖<b>已有的官方曲</b>是无效的（layeredfs 不覆盖本体文件），新增歌曲不受影响。</li>
</ul>

<h3>4.4 字体与缺字</h3>
<p>IIDX 的字库<b>没有韩文、也没有简体中文</b>。曲名或副标题里有这些字，游戏里会显示成空白或方块。</p>
<p>转换页会检测，出现时给黄色提示，「自动转写」按钮可以：韩文→罗马字、中文→拼音。
副标题里的缺字也会提示（建议副标题只用日文/英文）。</p>
<p>字体号默认自动：纯 ASCII → 0，含日文/中文 → 2。</p>

<h3>4.5 文字编码</h3>
<p>BMS 文件可能是 cp932（日文）、cp949（韩文）、UTF-8、UTF-16、EUC-JP、ISO-2022-JP。
程序会自动判断，转换页的预览信息里会写识别结果。乱码时优先检查这个。</p>

<h3>4.6 难度与等级</h3>
<ul>
<li><b>建议槽</b>：从谱面文件名猜的（<code>[ANOTHER]</code>、<code>_h</code>、<code>maniac</code> 等）。</li>
<li><b>作者等级</b>：谱面里 <code>#PLAYLEVEL</code> 写的数字。</li>
<li><b>推荐</b>：按物量密度算出来的 1–12 级，只作参考。</li>
<li>老代曲库没有 L / B 槽（26 代没有 BEGINNER、27 代以前没有 LEGGENDARIA），
勾了会被丢弃并提示。</li>
<li><b>转换写进曲库的等级一定是 1–12</b>：优先用作者写的 #PLAYLEVEL（超范围会夹到 1–12），
没写就用密度推荐值。列表里「等级」显示 <code>—</code> 只表示<b>原谱没写等级</b>，
不影响转换结果。</li>
</ul>

<h3>4.7 ID 是怎么分配的</h3>
<p>留空时按「放入 IIDX 文件夹」决定号段（1ST/SUBSTREAM→1xxx，第 n 代→nxxx），
在号段内<b>从高往低</b>找第一个空槽。想指定就自己填，冲突会报错。</p>
<p>槽位总数跟代次有关（25/26 是 26000，34 是 35000）。用原版（没有 omnimix）
时程序改的是 <code>music_data.bin</code>，同样从顶部往下找空位。</p>

<h3>4.8 备份、回收区、本机库的区别</h3>
<table>
<tr><th></th><th>存什么</th><th>在哪</th><th>怎么用</th></tr>
<tr><td>备份</td><td>曲库 + XML + 部署记录（整份快照）</td><td><code>library\\&lt;安装&gt;\\backups\\</code></td><td>出事整体回滚</td></tr>
<tr><td>回收区</td><td>被删的歌曲文件 + 那条曲库记录</td><td><code>contents</code> 同级 <code>bms2iidx_trash</code></td><td>删错了还原</td></tr>
<tr><td>本机库</td><td>每首歌的原始记录与封面</td><td><code>library\\&lt;安装&gt;\\</code></td><td>omni 包更新后写回曲库</td></tr>
</table>

<h3>4.9 BMS 谱面库索引</h3>
<p>列表按<b>歌曲</b>聚合（同目录的多个谱面算一首歌），双击一行即送到转换页挑谱面。
「更新索引」只对没变化的文件夹做跳过，通常 1 秒内完成；
「完整重建」会重读全部文件头（10 万谱面约 15–20 秒）。原地改过谱面内容、又没改过目录名时用完整重建。</p>

<h3>4.10 统计 NPS（时长 / 均值 / 峰值 / 推荐等级）</h3>
<p>索引平时只读文件头，所以这四个数**要单独点「统计 NPS…」才会算**。
点之前会弹窗告诉你：统计范围（当前筛选结果 / 整个库）、待算多少首、每首约多少秒、预计总时间。</p>
<ul>
<li>每首约 1 秒（单核；8 进程并行约 0.12 秒/首）</li>
<li>只在没算过的谱面上跑；算过的会跳过，文件改动后自动作废重算</li>
<li>算完列表才会出现「时长 / 均值 NPS / 峰值 NPS / 推荐等级」四列</li>
<li>推荐等级用的是和转换器同一套算法（`core/level.py`），所以和转换页看到的数字一致</li>
</ul>

<h3>4.11 两种游戏环境（omnimix / 原版）</h3>
<p>程序会自动判断你的游戏属于哪种，并写到对应位置：</p>

<table>
<tr><th></th><th>omnimix 环境</th><th>原版环境（没装 omni）</th></tr>
<tr><td>部署目录</td><td><code>data_mods/omnimix/</code></td><td><code>data_mods/bms2iidx/</code></td></tr>
<tr><td>曲库文件</td><td><code>info/0/music_omni.bin</code></td><td><code>info/0/music_data.bin</code></td></tr>
<tr><td>曲名/作者 XML</td><td><code>*_omni.merged.xml</code></td><td><code>*_yomi.merged.xml</code></td></tr>
<tr><td>判定依据</td><td>日志里 omnifix 加载了</td><td>没加载 omnifix</td></tr>
</table>

<p class="warn"><b>请选定一种环境，不要来回切换。</b>
两种环境用的是不同的曲库文件，切过去以后之前加的歌不会自动跟过去：
歌还在磁盘上，但游戏读的曲库里没有它。真要切换，先切好环境再
「检测」，然后在「IIDX 曲库与资源」页点「把记录写回曲库」把歌补进新环境的曲库。</p>

<p><b>两种环境都必须</b>用 <b>spice2x</b> 启动，并加载 <code>ifs_hook.dll</code>——
没有它，<code>data_mods</code> 里的东西游戏根本不会读。程序的「游戏环境」里会显示有没有加载，
没装就点「安装 ifs_hook.dll」。</p>

<p>已知限制（34 代实测，和本程序无关）：本体已有的 <b>wmv 影片无法被覆盖</b>；
<b>mp4 可以被覆盖</b>。本程序只做“新增歌曲”，两种格式的新增都正常，所以不受影响。</p>

<h3>4.12 快捷键</h3>
<table>
<tr><td><code>Ctrl+O</code></td><td>选 BMS 歌曲文件夹</td></tr>
<tr><td><code>Ctrl+Enter</code></td><td>把当前配置加进队列</td></tr>
<tr><td><code>F5</code></td><td>刷新 BMS 谱面库索引</td></tr>
<tr><td><code>Ctrl+L</code></td><td>折叠/展开底部日志</td></tr>
</table>

<h3>4.13 出错了怎么办</h3>
<p>先看日志。日志在 <code>library\logs\app.log</code>（帮助页顶部有
「打开日志文件」「打开日志文件夹」两个按钮）。日志里会记下每一步操作和完整的报错堆栈。</p>
<p>反馈问题时请附上：<b>你做了什么 + 日志最后 50 行</b>。有这两样，绝大多数问题不用复现就能定位。</p>

<h3>4.14 常见问题与报错</h3>
<table>
<tr><th>现象</th><th>多半是</th><th>怎么办</th></tr>
<tr><td>「…没有音源目录」</td><td>音源文件是空的 / 采样名解析错</td><td>看日志里第一个失败的采样名，检查谱面的 <code>#WAV</code> 路径</td></tr>
<tr><td>「号段 xxx–xxx 没有空位」</td><td>这个号段满了</td><td>换「放入 IIDX 文件夹」，或手动填一个空 ID</td></tr>
<tr><td>部署后游戏里没有歌</td><td>没重启 / 曲库槽位不对 / 挂载点没生效</td><td>重启游戏；看「游戏环境」里挂载点和槽位；确认 spice2x 加载了 ifs_hook</td></tr>
<tr><td>BGA 不播 / 全黑</td><td>代次容器不对，或引擎不吃这个编码</td><td>32 代以后必须 mp4；老代 wmv2 在 34 上实测可播</td></tr>
<tr><td>曲名是方块</td><td>字体号不对，或字库里没这个字</td><td>换字体号；韩文/简体换成罗马字/拼音</td></tr>
<tr><td>副标题不显示</td><td>曲库 <code>texture_subtitle</code> 字段为 0</td><td>本程序会自动写 2；旧版本转的歌需要重新转换或写回</td></tr>
<tr><td>BGA 变成 0.0 MB</td><td>编码失败</td><td>看日志里的 ffmpeg 报错；可先取消 BGA 只出图</td></tr>
<tr><td>提示“曲库在转换之后被改过”</td><td>转换完到部署之间，omni 包更新过</td><td>重新检测目标、重新转换，或强制部署</td></tr>
</table>

<h3>4.15 已知限制</h3>
<ul>
<li><b>纯原版（无 omnimix）的“覆盖基础曲库”这一步还没在真机验证</b>：
转换、部署、库管理都已跑通，但 <code>music_data.bin</code> / <code>*_yomi.merged.xml</code>
是覆盖游戏本来就有的文件，layeredfs 是否放行需要真实原版环境测一次
（影片覆盖官方曲目那次失败过，怀疑是影片加载不走 hook，不是 layeredfs 本身的问题）。
若实测不行，退路是直接改基础曲库（本程序会先自动备份）。</li>
<li>BPM 极端（<code>#BPM 0/1</code> 靠 stop 制造的变速）没有实测覆盖。</li>
<li>谱面时间轴对齐只在常规结构上验证过；长曲、密集变速的谱面建议先看试听。</li>
<li>25/26 代真机只验证过文件结构和 WMV8 解码，整首歌的实机手感还需你自己确认。</li>
<li>官方曲目、omnimix 自带曲目不能在这里删（菜单会禁掉），以免误删游戏本体。</li>
<li>本工具不包含任何 Konami 的资源文件，只做格式转换。</li>
</ul>

<p style="color:rgba(127,127,127,0.9);margin-top:18px">
遇到本页没写的情况：把底部日志里第一段红色报错整段复制出来，那是最有用的信息。
</p>
"""

# ---------------------------------------------------------------------- 关于
ABOUT_PAGE = """
<style>
  h2 { margin-top: 6px; margin-bottom: 4px; }
  h3 { margin-top: 16px; margin-bottom: 4px; }
  p, li { line-height: 150%; }
  code { background: rgba(127,127,127,0.16); padding: 1px 4px; }
</style>

<h2>BMS2IIDX BETA v0.1</h2>
<p>把 BMS 歌曲转换成 beatmania IIDX 能直接读的歌曲包。</p>

<h3>特别鸣谢</h3>
<p>高岭爱花老公　·　Necroxis Girl</p>

<h3>设计原则</h3>
<ul>
<li><b>尊重原作者</b>：画面不裁切、不放大、不插值；谱面按原谱转换，不改写作者的创作；
音源默认只转格式、不改音量。</li>
<li><b>不碰游戏本体</b>：不写 exe/dll、不改 spice2x 配置、不动存档，只往
<code>data_mods</code> 里加文件。</li>
<li><b>随时可回退</b>：部署前自动备份，删除进回收区，本机库存原始记录。</li>
<li><b>不猜</b>：拿不到的字段留空并在界面上说明，不编造数据填进去。</li>
</ul>

<h3>致谢</h3>
<ul>
<li><b>PySide6</b> —— 界面</li>
<li><b>ffmpeg</b> —— 视频/音频转码</li>
<li><b>ifstools</b> —— 图形包（ifs）读写</li>
<li><b>pypinyin</b> —— 中文转拼音（曲名缺字时用）</li>
<li><b>aixxe/omnifix</b> —— omnimix 运行时补丁层</li>
<li><b>mon/ifs_layeredfs</b> —— 文件层重定向（<code>ifs_hook.dll</code>）</li>
<li><b>drmext/iidx-datatools</b> —— 曲库格式与工具思路</li>
</ul>

<h3>交流反馈</h3>
<p>群：<b>1129787339</b></p>

"""
