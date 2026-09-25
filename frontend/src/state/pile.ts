import Matter from 'matter-js'

// The /search pile: one square rigid body per platform, dropped under gravity onto the floor of the
// page. The physics knows nothing about Vue or the DOM - every frame it hands back where each tile
// is, and the page moves its elements there. A tile that leaves the pile (it fits the job and flies
// to its card) is taken out of the world; one that comes back is dropped in again where it stands.
export type TilePose = { x: number, y: number, angle: number }   // top-left corner, radians
type FrameHandler = (poses: Map<string, TilePose>) => void

// The CSS for a pose, for an element whose transform-origin is its top-left corner. The body turns
// about its CENTRE, so the element must too: rotating about the corner put every tilted tile off
// its body, which read as tiles overlapping here and floating apart there.
export function poseTransform(p: TilePose, size: number) {
  const h = size / 2
  return `translate(${p.x + h}px,${p.y + h}px) rotate(${p.angle}rad) translate(${-h}px,${-h}px)`
}

const MAX_SPEED = 38   // px per step; a harder throw would tunnel through the floor

export class Pile {
  private engine = Matter.Engine.create({ enableSleeping: true, gravity: { x: 0, y: 1.1, scale: 0.001 } })
  private bodies = new Map<string, Matter.Body>()
  private walls: Matter.Body[] = []
  private raf = 0
  private grip: Matter.Constraint | null = null
  private held: Matter.Body | null = null
  private width = 0
  private height = 0
  size = 48

  constructor(private onFrame: FrameHandler) {}

  // The floor is the bottom edge of the page area and the walls its sides, so nothing leaves view.
  bounds(width: number, height: number) {
    this.width = width; this.height = height
    Matter.Composite.remove(this.engine.world, this.walls)
    const t = 200
    this.walls = [
      Matter.Bodies.rectangle(width / 2, height + t / 2, width * 3, t, { isStatic: true }),
      Matter.Bodies.rectangle(-t / 2, height / 2 - height, t, height * 4, { isStatic: true }),
      Matter.Bodies.rectangle(width + t / 2, height / 2 - height, t, height * 4, { isStatic: true }),
    ]
    Matter.Composite.add(this.engine.world, this.walls)
    for (const b of this.bodies.values()) Matter.Sleeping.set(b, false)
  }

  // Drop a tile with its top-left corner at (x, y). No position means "somewhere above the page".
  add(id: string, x?: number, y?: number, angle = 0) {
    if (this.bodies.has(id)) return
    const s = this.size
    const cx = x == null ? s / 2 + Math.random() * Math.max(1, this.width - s) : x + s / 2
    const cy = y == null ? -s - Math.random() * this.height * 0.6 : y + s / 2
    const body = Matter.Bodies.rectangle(cx, cy, s, s, {
      chamfer: { radius: s * 0.22 }, restitution: 0.12, friction: 0.55, frictionStatic: 0.9,
      frictionAir: 0.012, density: 0.0016, angle, label: id,
    })
    // Heavy in rotation: tiles tip and settle a little askew instead of spinning onto their corners,
    // so the pile reads as stacked tiles rather than scattered debris.
    Matter.Body.setInertia(body, body.inertia * 8)
    this.bodies.set(id, body)
    Matter.Composite.add(this.engine.world, body)
    this.start()
  }

  // Where a tile lies now, for an element that appears after its body stopped moving (a sleeping
  // body is not re-emitted every frame).
  poseOf(id: string): TilePose | null {
    const b = this.bodies.get(id)
    return b ? this.pose(b) : null
  }

  // Take a tile out of the world and say where it was, so its element can fly on from there.
  remove(id: string): TilePose | null {
    const b = this.bodies.get(id); if (!b) return null
    Matter.Composite.remove(this.engine.world, b)
    this.bodies.delete(id)
    for (const other of this.bodies.values()) Matter.Sleeping.set(other, false)   // what it held up may fall
    this.start()
    return this.pose(b)
  }

  clear() {
    Matter.Composite.remove(this.engine.world, [...this.bodies.values()])
    this.bodies.clear()
  }

  // A small upward kick: the tiles the search is reading hop in place.
  poke(ids: string[]) {
    for (const id of ids) {
      const b = this.bodies.get(id); if (!b) continue
      Matter.Sleeping.set(b, false)
      Matter.Body.setVelocity(b, { x: (Math.random() - 0.5) * 2.4, y: -5 - Math.random() * 3 })
      Matter.Body.setAngularVelocity(b, (Math.random() - 0.5) * 0.12)
    }
    this.start()
  }

  // Pick a tile up at a point (page-area coordinates): a spring from the pointer to the spot on the
  // tile that was grabbed, so the tile swings from where it is held. Returns false for a tile that
  // is not in the pile (one sitting on an answer card).
  grab(id: string, x: number, y: number) {
    const b = this.bodies.get(id); if (!b) return false
    this.release()
    Matter.Sleeping.set(b, false)
    this.held = b
    this.grip = Matter.Constraint.create({
      pointA: { x, y }, bodyB: b, pointB: { x: x - b.position.x, y: y - b.position.y },
      length: 0, stiffness: 0.18, damping: 0.08,
    })
    Matter.Composite.add(this.engine.world, this.grip)
    this.start()
    return true
  }

  drag(x: number, y: number) {
    if (!this.grip || !this.held) return
    this.grip.pointA = { x, y }
    Matter.Sleeping.set(this.held, false)
    this.start()
  }

  // Let go: the tile keeps the speed the pointer gave it, and flies.
  release() {
    if (this.grip) Matter.Composite.remove(this.engine.world, this.grip)
    this.grip = null; this.held = null
    for (const b of this.bodies.values()) Matter.Sleeping.set(b, false)
    this.start()
  }

  // Run the simulation to rest without drawing it, for reduced motion and resizes.
  settle(steps = 600) {
    for (let i = 0; i < steps && this.awake(); i++) Matter.Engine.update(this.engine, 1000 / 60)
    this.emit(true)
  }

  start() {
    if (this.raf) return
    // The first step is a nominal frame: a frame timestamp can precede the moment this loop was
    // started, and Matter reads a zero or negative step as "at rest" and puts every new body to
    // sleep where it spawned, above the page.
    let last = 0
    const tick = (now: number) => {
      Matter.Engine.update(this.engine, last ? Math.min(1000 / 30, Math.max(1, now - last)) : 1000 / 60)
      last = now
      for (const b of this.bodies.values()) {
        const v = b.velocity, speed = Math.hypot(v.x, v.y)
        if (speed > MAX_SPEED) Matter.Body.setVelocity(b, { x: v.x / speed * MAX_SPEED, y: v.y / speed * MAX_SPEED })
      }
      this.emit()
      this.raf = this.grip || this.awake() ? requestAnimationFrame(tick) : 0
    }
    this.raf = requestAnimationFrame(tick)
  }

  destroy() { cancelAnimationFrame(this.raf); this.raf = 0; Matter.Engine.clear(this.engine) }

  private awake() {
    for (const b of this.bodies.values()) if (!b.isSleeping) return true
    return false
  }

  private pose(b: Matter.Body): TilePose {
    return { x: b.position.x - this.size / 2, y: b.position.y - this.size / 2, angle: b.angle }
  }

  // The poses that changed: awake bodies only (a sleeping body has not moved), or all of them.
  private frame = new Map<string, TilePose>()
  private emit(all = false) {
    this.frame.clear()
    for (const [id, b] of this.bodies) if (all || !b.isSleeping) this.frame.set(id, this.pose(b))
    this.onFrame(this.frame)
  }
}

// Tile edge for a pile that fills about `share` of the page area's height: the tiles' area
// (loosely packed) spread over the page width.
export function tileSize(width: number, height: number, count: number, share = 0.3, min = 22) {
  const s = Math.sqrt((share * height * 0.7 * width) / Math.max(1, count))
  return Math.round(Math.max(min, Math.min(58, s)))
}
