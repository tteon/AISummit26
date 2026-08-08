//! Smoke test: DozerDB (FinBench) over the `neo4j` Rust driver.
//!
//! Three stages, each one step closer to the thread-per-agent harness:
//!   1. execute_query — managed transaction, one shot
//!   2. Session::transaction — explicit tx, parameterized query, row iteration
//!   3. thread-per-agent — N OS threads share one Arc<Driver>, each owns its
//!      session and transaction; per-thread wall time shows real parallelism.

use std::sync::Arc;
use std::time::Instant;

use neo4j::address::Address;
use neo4j::driver::auth::AuthToken;
use neo4j::driver::{ConnectionConfig, Driver, DriverConfig, RoutingControl};
use neo4j::retry::ExponentialBackoff;
use neo4j::session::SessionConfig;
use neo4j::{value_map, ValueReceive};

const WS: &str = "default";

fn make_driver() -> Driver {
    let address = Address::from(("127.0.0.1", 7687));
    let auth = AuthToken::new_basic_auth("neo4j", "neo4jpassword");
    Driver::new(
        ConnectionConfig::new(address),
        DriverConfig::new().with_auth(Arc::new(auth)),
    )
}

fn count_accounts(driver: &Driver, database: Arc<String>) -> i64 {
    let result = driver
        .execute_query("MATCH (a:Account {_workspace_id:$ws}) RETURN count(a) AS n")
        .with_database(database)
        .with_routing_control(RoutingControl::Read)
        .with_parameters(value_map!({"ws": WS}))
        .run_with_retry(ExponentialBackoff::default())
        .expect("count query failed");
    match result.into_scalar() {
        Ok(ValueReceive::Integer(n)) => n,
        other => panic!("unexpected scalar: {other:?}"),
    }
}

fn explicit_tx_top_transfers(driver: &Driver, database: Arc<String>) -> Vec<(String, f64)> {
    let mut session = driver.session(SessionConfig::new().with_database(database));
    session
        .transaction()
        .with_routing_control(RoutingControl::Read)
        .run_with_retry(ExponentialBackoff::default(), |tx| {
            let mut rows = Vec::new();
            let mut stream = tx
                .query(
                    "MATCH (a:Account {_workspace_id:$ws})-[t:TRANSFER]->(:Account {_workspace_id:$ws}) \
                     RETURN a.acct_no AS acct, t.amount AS amount \
                     ORDER BY t.amount DESC LIMIT $limit",
                )
                .with_parameters(value_map!({"ws": WS, "limit": 5}))
                .run()?;
            for record in stream.by_ref() {
                let mut record = record?;
                let acct = match record.take_value("acct") {
                    Some(ValueReceive::String(s)) => s,
                    Some(ValueReceive::Integer(i)) => i.to_string(),
                    other => format!("{other:?}"),
                };
                let amount = match record.take_value("amount") {
                    Some(ValueReceive::Float(f)) => f,
                    Some(ValueReceive::Integer(i)) => i as f64,
                    other => panic!("unexpected amount: {other:?}"),
                };
                rows.push((acct, amount));
            }
            stream.consume()?;
            tx.commit()?;
            Ok(rows)
        })
        .expect("explicit tx failed")
}

/// One "agent": its own session, its own transaction, a fixed read workload.
fn agent_workload(driver: &Driver, database: Arc<String>, agent_id: usize) -> (usize, usize, f64) {
    let start = Instant::now();
    let mut session = driver.session(SessionConfig::new().with_database(database));
    let rows = session
        .transaction()
        .with_routing_control(RoutingControl::Read)
        .run_with_retry(ExponentialBackoff::default(), |tx| {
            let mut n = 0usize;
            let mut stream = tx
                .query(
                    "MATCH (a:Account {_workspace_id:$ws})-[t:TRANSFER]->(b:Account {_workspace_id:$ws}) \
                     RETURN a.acct_no, t.amount, t.channel_risk, b.acct_no LIMIT $limit",
                )
                .with_parameters(value_map!({"ws": WS, "limit": 20_000}))
                .run()?;
            for record in stream.by_ref() {
                let _ = record?;
                n += 1;
            }
            stream.consume()?;
            tx.commit()?;
            Ok(n)
        })
        .expect("agent tx failed");
    (agent_id, rows, start.elapsed().as_secs_f64() * 1000.0)
}

fn main() {
    let db = Arc::new(String::from(
        std::env::args().nth(1).unwrap_or_else(|| "finbenchl1".into()),
    ));
    let n_agents: usize = std::env::args()
        .nth(2)
        .and_then(|s| s.parse().ok())
        .unwrap_or(4);

    let driver = Arc::new(make_driver());

    // 1. managed one-shot
    let t = Instant::now();
    let n = count_accounts(&driver, Arc::clone(&db));
    println!(
        "[1] execute_query        {db}: {n} accounts ({:.1} ms)",
        t.elapsed().as_secs_f64() * 1000.0
    );

    // 2. explicit transaction
    let t = Instant::now();
    let top = explicit_tx_top_transfers(&driver, Arc::clone(&db));
    println!(
        "[2] session+explicit tx  top transfers ({:.1} ms):",
        t.elapsed().as_secs_f64() * 1000.0
    );
    for (acct, amount) in &top {
        println!("      {acct} -> {amount:.2}");
    }

    // 3. thread-per-agent
    let t = Instant::now();
    let handles: Vec<_> = (0..n_agents)
        .map(|i| {
            let driver = Arc::clone(&driver);
            let db = Arc::clone(&db);
            std::thread::spawn(move || agent_workload(&driver, db, i))
        })
        .collect();
    let mut results: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    results.sort_by_key(|(id, ..)| *id);
    let wall = t.elapsed().as_secs_f64() * 1000.0;
    println!("[3] thread-per-agent     {n_agents} threads, one session+tx each:");
    let mut total_rows = 0usize;
    for (id, rows, ms) in &results {
        println!("      agent {id}: {rows} rows in {ms:.1} ms");
        total_rows += rows;
    }
    println!(
        "      wall {wall:.1} ms, {total_rows} rows total, {:.0} rows/s aggregate",
        total_rows as f64 / (wall / 1000.0)
    );
}
