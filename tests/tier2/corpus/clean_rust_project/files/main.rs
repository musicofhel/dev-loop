fn fibonacci(n: u32) -> u64 {
    match n {
        0 => 0,
        1 => 1,
        _ => {
            let mut a: u64 = 0;
            let mut b: u64 = 1;
            for _ in 2..=n {
                let temp = b;
                b = a + b;
                a = temp;
            }
            b
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fib_zero() {
        assert_eq!(fibonacci(0), 0);
    }

    #[test]
    fn test_fib_one() {
        assert_eq!(fibonacci(1), 1);
    }

    #[test]
    fn test_fib_ten() {
        assert_eq!(fibonacci(10), 55);
    }
}
