(() => {
  "use strict";
  if (location.protocol === "file:") return;
  const canvas = document.getElementById("thermalCanvas");
  if (!canvas) return;

  const WIDTH = 80, HEIGHT = 62, META_BYTES = 16, PAYLOAD_BYTES = META_BYTES + WIDTH * HEIGHT * 2, FREEZE_MS = 3000, POLL_MS = 150;
  const context = canvas.getContext("2d");
  const source = document.createElement("canvas");
  source.width = WIDTH; source.height = HEIGHT;
  const sourceContext = source.getContext("2d", { alpha:false });
  const image = sourceContext.createImageData(WIDTH, HEIGHT);
  const panel = document.getElementById("thermalPanel") || canvas.parentElement;
  const stops = [[0,0,0,4],[.18,42,10,91],[.36,101,21,110],[.54,159,42,99],[.70,221,81,58],[.86,252,165,10],[1,252,255,164]];
  let etag = null, fetching = false, lastSpace = null, scaleMin = null, scaleMax = null;
  let lastFrameAt = 0, lastSequence = null;

  function currentId() {
    if (document.body.dataset.spaceId) return document.body.dataset.spaceId;
    try { return typeof currentSpaceId === "string" ? currentSpaceId : ""; } catch { return ""; }
  }
  function colour(value) {
    const x = Math.max(0, Math.min(1, value));
    for (let i=1;i<stops.length;i++) if (x <= stops[i][0]) {
      const left=stops[i-1], right=stops[i], ratio=(x-left[0])/(right[0]-left[0]);
      return [1,2,3].map(c => Math.round(left[c]+(right[c]-left[c])*ratio));
    }
    return [252,255,164];
  }
  function resize() {
    const box=canvas.getBoundingClientRect(), ratio=window.devicePixelRatio||1;
    const width=Math.max(1,Math.round(box.width*ratio)), height=Math.max(1,Math.round(box.height*ratio));
    if (canvas.width!==width || canvas.height!==height) { canvas.width=width; canvas.height=height; }
  }
  function text(id, value) { const element=document.getElementById(id); if(element) element.textContent=value; }
  function unavailable(reason) {
    panel.classList.remove("live"); panel.classList.add("no-data");
    context.clearRect(0,0,canvas.width,canvas.height);
    text("thermalMax","수신 대기"); text("thermalAverage","No Data"); text("thermalFps","No Data");
    text("thermalMatch",reason); text("guestThermalStatus",reason);
  }
  function draw(buffer) {
    if (buffer.byteLength!==PAYLOAD_BYTES) throw Error(`열화상 패킷 길이 오류: ${buffer.byteLength}`);
    const view=new DataView(buffer), width=view.getUint16(0,false), height=view.getUint16(2,false), sequence=view.getUint32(4,false);
    if(width!==WIDTH||height!==HEIGHT) throw Error(`열화상 해상도 오류: ${width}×${height}`);
    const temperatures=new Float32Array(WIDTH*HEIGHT), valid=[];
    for(let i=0;i<temperatures.length;i++) { const c=view.getUint16(META_BYTES+i*2,false)*.1-273.15; temperatures[i]=c; if(c>=-40&&c<=150)valid.push(c); }
    if(valid.length<temperatures.length*.95) throw Error(`비정상 온도 데이터 ${valid.length}/${temperatures.length}`);
    valid.sort((a,b)=>a-b); let low=valid[Math.floor(valid.length*.02)], high=valid[Math.floor(valid.length*.98)];
    if(high-low<2){const centre=(low+high)/2;low=centre-1;high=centre+1;}
    scaleMin=scaleMin===null?low:scaleMin*.82+low*.18; scaleMax=scaleMax===null?high:scaleMax*.82+high*.18;
    const range=Math.max(.1,scaleMax-scaleMin);
    for(let i=0;i<temperatures.length;i++){const [r,g,b]=colour((temperatures[i]-scaleMin)/range), o=i*4; image.data[o]=r;image.data[o+1]=g;image.data[o+2]=b;image.data[o+3]=255;}
    sourceContext.putImageData(image,0,0); resize(); context.imageSmoothingEnabled=true; context.imageSmoothingQuality="high"; context.drawImage(source,0,0,canvas.width,canvas.height);
    if(sequence!==lastSequence) lastFrameAt=performance.now(); lastSequence=sequence;
    panel.classList.add("live"); panel.classList.remove("no-data");
    text("thermalMax",`최고 ${high.toFixed(1)}℃`); text("thermalAverage",`범위 ${low.toFixed(1)}~${high.toFixed(1)}℃`); text("thermalFps",`Frame ${sequence}`); text("thermalMatch","ESP32 실시간 열화상");
    text("guestThermalStatus",`실시간 수신 · Frame ${sequence} · ${low.toFixed(1)}~${high.toFixed(1)}℃`);
  }
  async function update() {
    if(fetching)return; fetching=true;
    try {
      const id=currentId(); if (!id) { unavailable("열화상 프레임 대기"); return; } if(id!==lastSpace){lastSpace=id;etag=null;scaleMin=null;scaleMax=null;lastFrameAt=0;lastSequence=null;unavailable("열화상 프레임 대기");}
      const response=await fetch(`/api/thermal/${encodeURIComponent(id)}`,{cache:"no-store",headers:etag?{"If-None-Match":etag}:{}});
      if(response.status===204){unavailable("열화상 프레임 대기");return;}
      if(response.status===304){if(!lastFrameAt||performance.now()-lastFrameAt>FREEZE_MS)unavailable("열화상 수신 중단");return;}
      if(!response.ok)throw Error(`HTTP ${response.status}`);
      draw(await response.arrayBuffer()); etag=response.headers.get("ETag")||etag;
    } catch(error) { unavailable(`열화상 데이터 오류 · ${error.message}`); }
    finally { fetching=false; }
  }
  window.addEventListener("resize",resize); resize(); update(); setInterval(update,POLL_MS);
  setInterval(()=>{if(lastFrameAt&&performance.now()-lastFrameAt>FREEZE_MS)unavailable("열화상 수신 중단");},500);
})();


