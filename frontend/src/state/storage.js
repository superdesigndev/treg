// Browser storage that never throws. Safari with site data blocked (and some embedded views) throw
// on the first touch of `localStorage`, even reading the property; the dashboard treats every
// stored value as a convenience, so a read falls back to null and a write is dropped.
function area(){ try{ return window.localStorage; }catch(e){ return null; } }

export function storageGet(key){ try{ return area()?.getItem(key) ?? null; }catch(e){ return null; } }
export function storageSet(key, value){ try{ area()?.setItem(key, value); }catch(e){} }
export function storageRemove(key){ try{ area()?.removeItem(key); }catch(e){} }
