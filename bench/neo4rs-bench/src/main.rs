//! The native end of the driver spectrum: one process, no GIL, rows owned by Rust.
//!
//! Replicates bench_driver_memory.py's concurrency stage exactly — 2,000-row UNWIND calls,
//! 25 per worker, workers ∈ {1,2,4,8} — inside a single OS process on the tokio
//! multi-threaded runtime. The Python thread stage plateaus near 1.3 cores because every
//! decoded row must pass through the interpreter; if a single native process scales here,
//! the ceiling was the runtime, and one native data plane per box does what Python needed
//! a process per worker to do.
//!
//!   NEO4J_PASSWORD=... cargo run --release -- [database]

use std::time::Instant;

const ROWS: i64 = 2_000;
const CALLS_PER_WORKER: usize = 25;
const WORKERS: [usize; 4] = [1, 2, 4, 8];
const QUERY: &str = "UNWIND range(1,$n) AS i RETURN i, toString(i) AS s, i*1.5 AS f";

fn cpu_seconds() -> f64 {
    // utime + stime from /proc/self/stat (fields 14 and 15), all threads of this process.
    let stat = std::fs::read_to_string("/proc/self/stat").unwrap();
    let after = &stat[stat.rfind(')').unwrap() + 1..];
    let f: Vec<&str> = after.split_whitespace().collect();
    let ticks: u64 = f[11].parse::<u64>().unwrap() + f[12].parse::<u64>().unwrap();
    ticks as f64 / 100.0
}

async fn one_call(graph: &neo4rs::Graph) -> f64 {
    let t0 = Instant::now();
    let mut result = graph
        .execute(neo4rs::query(QUERY).param("n", ROWS))
        .await
        .expect("query failed");
    // Materialize every row into owned Rust values, mirroring the Python side's list of
    // dicts — the comparison is about who owns the rows, not about skipping the work.
    let mut rows: Vec<(i64, String, f64)> = Vec::with_capacity(ROWS as usize);
    while let Ok(Some(row)) = result.next().await {
        rows.push((
            row.get::<i64>("i").unwrap(),
            row.get::<String>("s").unwrap(),
            row.get::<f64>("f").unwrap(),
        ));
    }
    assert_eq!(rows.len(), ROWS as usize);
    t0.elapsed().as_secs_f64() * 1000.0
}

#[tokio::main]
async fn main() {
    let password = std::env::var("NEO4J_PASSWORD").expect("set NEO4J_PASSWORD");
    let database = std::env::args().nth(1).unwrap_or_else(|| "neo4j".into());
    let config = neo4rs::ConfigBuilder::default()
        .uri("bolt://localhost:7687")
        .user("neo4j")
        .password(&password)
        .db(database.as_str())
        .fetch_size(1000)
        .build()
        .unwrap();
    let graph = neo4rs::Graph::connect(config).await.expect("connect failed");

    one_call(&graph).await; // warm pool + plan cache

    println!("neo4rs native bench — single process, tokio multi-thread runtime, db={database}");
    for &w in WORKERS.iter() {
        let cpu0 = cpu_seconds();
        let wall0 = Instant::now();
        let mut handles = Vec::new();
        for _ in 0..w {
            let g = graph.clone();
            handles.push(tokio::spawn(async move {
                let mut lat = Vec::with_capacity(CALLS_PER_WORKER);
                for _ in 0..CALLS_PER_WORKER {
                    lat.push(one_call(&g).await);
                }
                lat
            }));
        }
        let mut lat: Vec<f64> = Vec::new();
        for h in handles {
            lat.extend(h.await.unwrap());
        }
        let wall = wall0.elapsed().as_secs_f64();
        let cpu = cpu_seconds() - cpu0;
        lat.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let p50 = lat[lat.len() / 2];
        let p99 = lat[((lat.len() as f64 * 0.99) as usize).min(lat.len() - 1)];
        println!(
            "  workers={w:<2} (neo4rs)  p50 {p50:7.1} ms   p99 {p99:7.1} ms   \
             client-util {:4.0}%   cpu/call client {:5.1} ms",
            cpu / wall * 100.0,
            cpu * 1000.0 / lat.len() as f64
        );
    }
}
