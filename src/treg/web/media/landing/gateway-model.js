/* Parametric reconstruction from the approved raster reference, in metres.
   Physical backside details are inferred, not scanned. Shared by viewer/export. */
export function createGateway(T,font){
 const root=new T.Group();root.name='Treg layered gateway';
 const materials={
  // Art-directed porcelain/silver blend: white faces, more reflective sections.
  metal:new T.MeshPhysicalMaterial({name:'Glazed white porcelain',color:0xfafaf8,metalness:.3,roughness:.23,clearcoat:1,clearcoatRoughness:.16,transmission:0,ior:1.5,envMapIntensity:.85}),
  ceramicEdge:new T.MeshPhysicalMaterial({name:'Silver porcelain edge blend',color:0xe9eceb,metalness:.72,roughness:.17,clearcoat:.8,clearcoatRoughness:.12,transmission:0,ior:1.5,envMapIntensity:1.05}),
  chrome:new T.MeshStandardMaterial({name:'Polished edge silver',color:0xf0f2f1,metalness:1,roughness:.12}),
  glass:new T.MeshPhysicalMaterial({name:'Smoke jade glass',color:0x8bbeb0,metalness:0,roughness:.055,transmission:.94,thickness:.16,ior:1.48,attenuationColor:new T.Color(0x1d6250),attenuationDistance:.55,envMapIntensity:1.15,clearcoat:1}),
  dark:new T.MeshStandardMaterial({name:'Graphite engraved mark',color:0x203a32,roughness:.5,metalness:.35}),
  logoWhite:new T.MeshStandardMaterial({name:'Ivory logo inlay',color:0xf8faf8,roughness:.48,metalness:0}),
  light:new T.MeshStandardMaterial({name:'Mint light inlay',color:0x8ee8c8,emissive:0x45c597,emissiveIntensity:1.6,metalness:.15,roughness:.23})
 };
 function rounded(w,h,r,cy=0){const p=new T.Shape(),x=-w/2,y=-h/2+cy;p.moveTo(x+r,y);p.lineTo(x+w-r,y);p.quadraticCurveTo(x+w,y,x+w,y+r);p.lineTo(x+w,y+h-r);p.quadraticCurveTo(x+w,y+h,x+w-r,y+h);p.lineTo(x+r,y+h);p.quadraticCurveTo(x,y+h,x,y+h-r);p.lineTo(x,y+r);p.quadraticCurveTo(x,y,x+r,y);return p;}
 function frame(w,h,iw,ih,depth,bevel,innerY=0){const shape=rounded(w,h,.25),hole=rounded(iw,ih,.17,innerY);shape.holes.push(hole);const g=new T.ExtrudeGeometry(shape,{depth,bevelEnabled:true,bevelSegments:10,steps:1,bevelSize:bevel,bevelThickness:bevel,curveSegments:32});g.translate(0,0,-depth/2);return g;}
 function add(name,geometry,material,z,parent=root){const m=new T.Mesh(geometry,material);m.name=name;m.position.z=z;m.userData.baseZ=z;parent.add(m);return m;}
 const front=new T.Group();front.name='01 Front metal face';front.position.z=.66;front.userData.baseZ=.66;root.add(front);
 const frontGeometry=frame(2.6,2.6,1.83,1.69,.12,.022);
 // Actual recessed pockets: remove only the front cap, re-triangulate it with
 // logo openings, then add metal cavity walls and a floor below the surface.
 const surfaceZ=.082,pocketDepth=.012,logoScale=.48,logoX=-.75,logoY=1.075;
 function mapped(path,x=0,y=0){return new T.Shape(path.getPoints(16).map(p=>new T.Vector2(logoX+(p.x+x)*logoScale,logoY+(p.y+y)*logoScale)));}
 const badge=rounded(.24,.24,.055);
 badge.holes.push(rounded(.082,.082,0,.041));
 badge.holes[0].curves.forEach(c=>{for(const k of ['v0','v1','v2','v3'])if(c[k])c[k].x-=.041;});
 const lower=rounded(.082,.082,0,-.041);
 lower.curves.forEach(c=>{for(const k of ['v0','v1','v2','v3'])if(c[k])c[k].x+=.041;});badge.holes.push(lower);
 const paths=[{shape:badge,x:0,y:0}];
 if(font){const letters=font.generateShapes('treg',.17),g=new T.ShapeGeometry(letters);g.computeBoundingBox();const offset=-(g.boundingBox.min.y+g.boundingBox.max.y)/2;g.dispose();letters.forEach(shape=>paths.push({shape,x:.195,y:offset}));}
 const pockets=paths.map(({shape,x,y})=>{const p=mapped(shape,x,y);p.holes=shape.holes.map(h=>mapped(h,x,y));return p;});
 const attributes=Object.fromEntries(Object.keys(frontGeometry.attributes).map(k=>[k,[]])),groups=[];
 for(const group of frontGeometry.groups){const start=attributes.position.length/3;
  for(let i=group.start;i<group.start+group.count;i+=3){
   if([0,1,2].every(n=>Math.abs(frontGeometry.attributes.position.getZ(i+n)-surfaceZ)<1e-5))continue;
   for(const [name,values] of Object.entries(attributes)){const a=frontGeometry.attributes[name];for(let n=0;n<3;n++)for(let c=0;c<a.itemSize;c++)values.push(a.array[(i+n)*a.itemSize+c]);}
  }
  groups.push({start,count:attributes.position.length/3-start,materialIndex:group.materialIndex});
 }
 for(const [name,values] of Object.entries(attributes))frontGeometry.setAttribute(name,new T.Float32BufferAttribute(values,frontGeometry.attributes[name].itemSize));
 frontGeometry.clearGroups();groups.forEach(g=>frontGeometry.addGroup(g.start,g.count,g.materialIndex));
 add('Solid rounded front face',frontGeometry,[materials.metal,materials.ceramicEdge],0,front);
 const cap=rounded(2.6,2.6,.25);cap.holes.push(rounded(1.83,1.69,.17),...pockets);
 add('Front face with engraved logo openings',new T.ShapeGeometry(cap,32),materials.metal,surfaceZ,front);
 const engraving=materials.metal.clone();engraving.name='Recessed porcelain mark';engraving.color.setHex(0x78847e);engraving.transmission=0;engraving.roughness=.62;engraving.clearcoat=.08;engraving.envMapIntensity=.35;engraving.side=T.DoubleSide;
 materials.engraving=engraving;
 for(const pocket of pockets){
  const walls=new T.ExtrudeGeometry(pocket,{depth:pocketDepth,bevelEnabled:false,curveSegments:16});
  const sides=walls.groups.find(g=>g.materialIndex===1);walls.clearGroups();walls.setDrawRange(sides.start,sides.count);
  add('Engraved logo cavity walls',walls,engraving,surfaceZ-pocketDepth,front);
  add('Engraved logo pocket floor',new T.ShapeGeometry(pocket,16),engraving,surfaceZ-pocketDepth,front);
  for(const island of pocket.holes)add('Logo uncut metal island',new T.ShapeGeometry(new T.Shape(island.getPoints(16))),materials.metal,surfaceZ,front);
 }
 const glassGeometry=frame(2.61,2.61,1.98,1.84,.085,.014);
 const trimGeometry=frame(2.635,2.635,2.565,2.565,.016,.005);
 for(let i=0;i<6;i++){
  const layer=new T.Group();layer.name=`Glass cassette ${i+1}`;layer.position.z=.43-i*.205;layer.userData.baseZ=layer.position.z;root.add(layer);
  add('Jade optical frame',glassGeometry,materials.glass,0,layer);
  add('Polished outer rim',trimGeometry,materials.chrome,.038,layer);
  for(const side of [-1,1]){const l=new T.Mesh(new T.BoxGeometry(.035,.022,.07),materials.light);l.position.set(side*1.27,-.02,.032);layer.add(l);}
 }
 add('Rear aluminium frame',frame(2.6,2.6,1.83,1.69,.075,.018),[materials.metal,materials.ceramicEdge],-.84);
 root.userData={description:'Real bevelled frames with open apertures; 6 glass cassettes, white porcelain faces with silver-blended sidewalls and bevels.',units:'metres'};
 return {root,materials};
}
