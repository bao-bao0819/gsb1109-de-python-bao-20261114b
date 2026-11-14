typedef unsigned int uint32;
typedef unsigned char uint8;

struct LinkA {
    int code;
    struct LinkA *next;
};

typedef struct HeaderA {
    uint32 magic;
    struct LinkA *link;
    struct HeaderA *owner;
} HeaderA;

typedef struct {
    uint32 ts;
    uint8 level;
    HeaderA hdr;
    char msg[32];
    uint32 id;
} Payload;

typedef int Status;

struct WrapA {
    Status s;
};

typedef struct {
    uint32 outer_field;
} LocalRec;

void scope_demo_a(void)
{
    typedef struct {
        uint8 a;
        uint8 b;
    } LocalRec;

    struct BoxA {
        LocalRec *r;
        int tag;
    };
}
